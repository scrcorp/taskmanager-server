"""미가입(유령) 계정 흡수(absorb) — 인수 코드를 안 쓰고 따로 가입해버린 경우의 폴백.

정상 경로는 claim(auth_service._claim_provisional_account)이다. 그 경우엔 행을 그대로
이어받으므로 데이터 이동이 아예 없다. 이 모듈은 **직원이 코드를 무시하고 새로 가입해
계정이 2개가 된 경우**에만 쓴다.

일반적인 "계정 병합"과 달리 범위가 좁다: 유령은 로그인이 불가능하므로 본인이 만든 데이터
(출근 기록, 본인 작성 리포트/코멘트 등)가 원천적으로 없고, **관리자가 붙여준 것만** 있다.
그래서 옮길 대상이 아래 목록으로 한정된다.

임포트 도구와 같은 철학: blind merge 금지 → preview(무엇이 옮겨지고 무엇이 충돌하는지)
→ 운영자 확인 → commit(단일 트랜잭션).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org_member import OrgMember, OrgMemberStore
from app.models.user import User
from app.models.user_store import UserStore
from app.utils.exceptions import BadRequestError, NotFoundError

# (user, X) unique 를 가진 테이블 — 대상이 같은 키를 이미 갖고 있으면 유령 행을 버린다.
# (모델, user 컬럼명, 충돌 판정에 함께 쓰는 컬럼들)
_UNIQUE_SCOPED: list[tuple[str, str, tuple[str, ...]]] = [
    ("app.models.availability:StaffAvailability", "user_id", ("organization_id", "day_of_week")),
    ("app.models.settings:StaffSetting", "user_id", ("key",)),
]

# 단순 UPDATE 로 옮기면 되는 것들 — (모델 경로, user 컬럼명)
_SIMPLE_MOVE: list[tuple[str, str]] = [
    ("app.models.schedule:Schedule", "user_id"),
    ("app.models.warning:Warning", "subject_user_id"),
    ("app.models.evaluation:Evaluation", "evaluatee_id"),
]


def _load(path: str):
    """'module:Class' → 클래스. 모델이 없는 배포에서도 죽지 않게 None 반환."""
    mod_name, _, cls_name = path.partition(":")
    try:
        mod = __import__(mod_name, fromlist=[cls_name])
        return getattr(mod, cls_name, None)
    except ImportError:
        return None


@dataclass
class AbsorbPlan:
    """흡수 계획 — preview 와 commit 이 공유하는 요약."""

    provisional_name: str = ""
    target_name: str = ""
    moves: dict[str, int] = field(default_factory=dict)      # 테이블 → 옮길 행 수
    store_transfers: list[dict] = field(default_factory=list)  # {store_name, empid, action}
    conflicts: list[str] = field(default_factory=list)         # 사람이 읽는 충돌 설명
    crewid_action: str = ""                                    # 승계/유지 결과 설명


async def _resolve_pair(
    db: AsyncSession, organization_id: UUID, provisional_id: UUID, target_id: UUID
) -> tuple[User, User]:
    """유령/대상 유저 검증 — org 스코프, 역할 뒤바뀜 방지."""
    if provisional_id == target_id:
        raise BadRequestError("Cannot absorb an account into itself")
    rows = (
        await db.execute(
            select(User).where(
                User.id.in_([provisional_id, target_id]),
                User.organization_id == organization_id,
            )
        )
    ).scalars().all()
    by_id = {u.id: u for u in rows}
    ghost = by_id.get(provisional_id)
    target = by_id.get(target_id)
    if ghost is None or target is None:
        raise NotFoundError("User not found in this organization")
    if not ghost.is_provisional:
        raise BadRequestError("Source account is not a provisional account")
    if target.is_provisional:
        raise BadRequestError("Target account must be a real (signed-up) account")
    return ghost, target


async def _members(
    db: AsyncSession, organization_id: UUID, ghost: User, target: User
) -> tuple[OrgMember | None, OrgMember | None]:
    rows = (
        await db.execute(
            select(OrgMember).where(
                OrgMember.organization_id == organization_id,
                OrgMember.user_id.in_([ghost.id, target.id]),
            )
        )
    ).scalars().all()
    g = next((m for m in rows if m.user_id == ghost.id), None)
    t = next((m for m in rows if m.user_id == target.id), None)
    return g, t


async def preview_absorb(
    db: AsyncSession, organization_id: UUID, provisional_id: UUID, target_id: UUID
) -> AbsorbPlan:
    """무엇이 옮겨지고 무엇이 충돌하는지 — DB 변경 없음."""
    ghost, target = await _resolve_pair(db, organization_id, provisional_id, target_id)
    plan = AbsorbPlan(provisional_name=ghost.full_name, target_name=target.full_name)

    g_member, t_member = await _members(db, organization_id, ghost, target)

    # crewid — 대상이 없을 때만 승계 (있으면 대상 값 유지)
    if g_member is not None and g_member.crewid is not None:
        if t_member is None or t_member.crewid is None:
            plan.crewid_action = f"CREWID {g_member.crewid} will move to the target"
        else:
            plan.crewid_action = (
                f"target keeps CREWID {t_member.crewid}; "
                f"provisional CREWID {g_member.crewid} is dropped"
            )

    # 매장 배정 + empid
    if g_member is not None:
        from app.models.organization import Store

        g_stores = (
            await db.execute(
                select(OrgMemberStore, Store.name)
                .join(Store, Store.id == OrgMemberStore.store_id)
                .where(OrgMemberStore.org_member_id == g_member.id)
            )
        ).all()
        t_store_ids: set[UUID] = set()
        if t_member is not None:
            t_store_ids = set(
                (
                    await db.execute(
                        select(OrgMemberStore.store_id).where(
                            OrgMemberStore.org_member_id == t_member.id
                        )
                    )
                ).scalars().all()
            )
        for ms, store_name in g_stores:
            if ms.store_id in t_store_ids:
                plan.store_transfers.append(
                    {"store_name": store_name, "empid": ms.empid, "action": "keep_target"}
                )
                plan.conflicts.append(
                    f"{store_name}: target is already assigned — target's number is kept, "
                    f"provisional #{ms.empid} is dropped"
                )
            else:
                plan.store_transfers.append(
                    {"store_name": store_name, "empid": ms.empid, "action": "move"}
                )

    # 단순 이동 대상 건수
    for path, col in _SIMPLE_MOVE:
        model = _load(path)
        if model is None:
            continue
        n = (
            await db.execute(
                select(func_count(model)).where(getattr(model, col) == ghost.id)
            )
        ).scalar() or 0
        if n:
            plan.moves[model.__tablename__] = n

    # (user, X) unique 테이블 — 충돌 건수만 알려준다
    for path, col, _keys in _UNIQUE_SCOPED:
        model = _load(path)
        if model is None:
            continue
        n = (
            await db.execute(
                select(func_count(model)).where(getattr(model, col) == ghost.id)
            )
        ).scalar() or 0
        if n:
            plan.moves[model.__tablename__] = n
    return plan


def func_count(model):
    """select(count) 헬퍼 — 모델별 PK 카운트."""
    from sqlalchemy import func

    return func.count(model.id)


async def absorb(
    db: AsyncSession, organization_id: UUID, provisional_id: UUID, target_id: UUID
) -> AbsorbPlan:
    """유령의 관리자 배정 데이터를 대상 계정으로 옮기고 유령 행을 폐기한다 (단일 트랜잭션)."""
    plan = await preview_absorb(db, organization_id, provisional_id, target_id)
    ghost, target = await _resolve_pair(db, organization_id, provisional_id, target_id)
    g_member, t_member = await _members(db, organization_id, ghost, target)

    try:
        # 채번 스코프 잠금 — empid 이동 중 동시 채번과 경합 방지
        from app.services.org_numbering import lock_empid_scope

        if g_member is not None:
            g_store_ids = list(
                (
                    await db.execute(
                        select(OrgMemberStore.store_id).where(
                            OrgMemberStore.org_member_id == g_member.id
                        )
                    )
                ).scalars().all()
            )
            for sid in g_store_ids:
                await lock_empid_scope(db, sid)

        # 1) org_member — 대상이 이미 소속이면 필드 승계, 아니면 행 자체를 넘긴다
        if g_member is not None:
            if t_member is None:
                g_member.user_id = target.id  # uq_org_member_user_org 위반 없음
                t_member = g_member
                g_member = None
            else:
                if t_member.crewid is None and g_member.crewid is not None:
                    crew = g_member.crewid
                    g_member.crewid = None  # partial unique 회피 — 먼저 비운다
                    await db.flush()
                    t_member.crewid = crew
                if t_member.hourly_rate is None and g_member.hourly_rate is not None:
                    t_member.hourly_rate = g_member.hourly_rate
                if t_member.department is None and g_member.department is not None:
                    t_member.department = g_member.department
                await db.flush()

        # 2) 매장 배정 + empid — 대상에 없는 매장만 넘기고, 겹치면 유령 쪽을 버린다
        if g_member is not None and t_member is not None:
            t_store_ids = set(
                (
                    await db.execute(
                        select(OrgMemberStore.store_id).where(
                            OrgMemberStore.org_member_id == t_member.id
                        )
                    )
                ).scalars().all()
            )
            g_rows = (
                await db.execute(
                    select(OrgMemberStore).where(
                        OrgMemberStore.org_member_id == g_member.id
                    )
                )
            ).scalars().all()
            if g_rows:
                # 승계 이력용 스냅샷 — 유령→실계정으로 번호가 넘어간 사실을 원장에 남긴다
                from app.core.client_surface import current_channel
                from app.models.empid_change import EMPID_SOURCE_ABSORB, EmpidChange
                from app.models.organization import Store as StoreModel

                store_names = {
                    r.id: r.name
                    for r in (
                        await db.execute(
                            select(StoreModel.id, StoreModel.name).where(
                                StoreModel.id.in_([g.store_id for g in g_rows])
                            )
                        )
                    ).all()
                }
            for row in g_rows:
                if row.store_id in t_store_ids:
                    await db.delete(row)  # 대상 번호 유지
                else:
                    row.org_member_id = t_member.id  # empid 그대로 승계
                    if row.empid is not None:
                        db.add(EmpidChange(
                            organization_id=organization_id,
                            store_id=row.store_id,
                            store_name=store_names.get(row.store_id),
                            user_id=target.id,
                            person_name=target.full_name,
                            old_empid=None, new_empid=row.empid,
                            source=EMPID_SOURCE_ABSORB,
                            channel=current_channel(), changed_by=None,
                        ))
            await db.flush()

        # 3) 레거시 user_stores — (user, store) unique 라 겹치면 버린다
        t_us = set(
            (
                await db.execute(
                    select(UserStore.store_id).where(UserStore.user_id == target.id)
                )
            ).scalars().all()
        )
        for us in (
            await db.execute(select(UserStore).where(UserStore.user_id == ghost.id))
        ).scalars().all():
            if us.store_id in t_us:
                await db.delete(us)
            else:
                us.user_id = target.id
        await db.flush()

        # 4) 단순 이동 — unique 충돌 없는 대상 데이터
        for path, col in _SIMPLE_MOVE:
            model = _load(path)
            if model is None:
                continue
            await db.execute(
                update(model)
                .where(getattr(model, col) == ghost.id)
                .values(**{col: target.id})
            )

        # 5) (user, X) unique 보유 — 대상에 같은 키가 있으면 유령 행 삭제 후 이동
        for path, col, keys in _UNIQUE_SCOPED:
            model = _load(path)
            if model is None:
                continue
            g_rows = (
                await db.execute(select(model).where(getattr(model, col) == ghost.id))
            ).scalars().all()
            if not g_rows:
                continue
            t_rows = (
                await db.execute(select(model).where(getattr(model, col) == target.id))
            ).scalars().all()
            t_keys = {tuple(getattr(r, k) for k in keys) for r in t_rows}
            for r in g_rows:
                if tuple(getattr(r, k) for k in keys) in t_keys:
                    await db.delete(r)
                else:
                    setattr(r, col, target.id)
            await db.flush()

        # 6) 유령 행 폐기 — username 을 비켜주고 소프트 삭제
        if g_member is not None:
            await db.execute(delete(OrgMember).where(OrgMember.id == g_member.id))
        ghost.is_active = False
        ghost.is_provisional = False
        ghost.claim_code = None
        ghost.deleted_at = datetime.now(timezone.utc)
        ghost.username = f"absorbed_{ghost.username}"[:100]
        await db.commit()
        return plan
    except Exception:
        await db.rollback()
        raise
