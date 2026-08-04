"""org 번호(crewid) / 매장 번호(empid) 다음 순번 계산.

crewid = org 안에서 1부터. empid = 채번 스코프 안에서 번호대 시작값(floor)부터 MAX+1.

채번 스코프(scope): 매장이 그룹(store_groups)에 속하고 numbering_mode="group" 이면
그룹 내 전체 매장(폐점 포함 — 휴면·폐점 번호도 점유), 그 외에는 해당 매장 하나.
번호대(floor): store.number_range_start > group.number_range_start > 1 순 폴백.

동시성: 단일 매장 스코프는 경합 시 (store_id, empid) partial unique 로 걸린다.
그룹 공유 스코프는 매장이 달라 인덱스로 못 막으므로 pg_advisory_xact_lock(그룹 키)으로
직렬화한다(트랜잭션 종료 시 자동 해제). 트라이얼 단계 수용.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org_member import OrgMember, OrgMemberStore

# 정책 A: 번호는 한 번 부여되면 고정. 배정 해제해도 행은 삭제하지 않고 휴면(플래그 false)으로
# 보존 → 재배정 시 같은 행/같은 empid 재사용. 휴면 행도 번호를 점유하므로 신규는 MAX+1 로 새 번호.
# (껐다 켰다 해도 empid 불변, 무한히 안 올라감.)


async def next_crewid(db: AsyncSession, organization_id: UUID) -> int:
    """org 안에서 다음 crewid — MAX+1 (1부터). 휴면 포함 사용 중 번호는 건너뜀."""
    return (
        await db.execute(
            select(func.coalesce(func.max(OrgMember.crewid), 0) + 1).where(
                OrgMember.organization_id == organization_id
            )
        )
    ).scalar() or 1


async def empid_scope_store_ids(db: AsyncSession, store_id: UUID) -> list[UUID]:
    """empid 채번 스코프의 매장 id 목록.

    그룹 소속 + numbering_mode="group" → 그룹 내 전체 매장(폐점 포함 — 번호 점유 유지).
    그 외(미그룹 or mode="store") → [store_id].
    """
    from app.models.organization import NUMBERING_MODE_GROUP, Store, StoreGroup

    row = (
        await db.execute(
            select(Store.group_id, StoreGroup.numbering_mode)
            .outerjoin(StoreGroup, StoreGroup.id == Store.group_id)
            .where(Store.id == store_id)
        )
    ).first()
    if row is None or row.group_id is None or row.numbering_mode != NUMBERING_MODE_GROUP:
        return [store_id]
    ids = (
        await db.execute(select(Store.id).where(Store.group_id == row.group_id))
    ).scalars().all()
    return list(ids) or [store_id]


async def _empid_floor(db: AsyncSession, store_id: UUID) -> int:
    """empid 번호대 시작값.

    - Shared(numbering_mode="group") 그룹: 그룹 번호대만 적용 — 매장 개별값은 무시한다.
      (그룹 = 하나의 공유 대역. UI 도 Shared 모드에선 매장별 입력을 숨긴다. 과거 Per-store
      시절 남은 매장값이 공유 시퀀스를 엉뚱한 대역으로 밀어올리는 것 방지 — QA 발견.)
    - Per-store 그룹: 매장값 > 그룹 기본값(Default range) > 1.
    - 미그룹: 매장값 > 1.
    """
    from app.models.organization import NUMBERING_MODE_GROUP, Store, StoreGroup

    row = (
        await db.execute(
            select(
                Store.number_range_start,
                StoreGroup.number_range_start.label("group_start"),
                StoreGroup.numbering_mode,
            )
            .outerjoin(StoreGroup, StoreGroup.id == Store.group_id)
            .where(Store.id == store_id)
        )
    ).first()
    if row is None:
        return 1
    if row.numbering_mode == NUMBERING_MODE_GROUP:
        return row.group_start or 1
    return row.number_range_start or row.group_start or 1


async def lock_empid_scope(db: AsyncSession, store_id: UUID) -> None:
    """그룹 공유 스코프면 그룹 키 advisory lock 선취 (트랜잭션 종료 시 자동 해제).

    단일 매장 스코프는 no-op — (store_id, empid) partial unique 가 2차 방어.
    같은 트랜잭션 내 재획득은 무해(재진입)라 next_empid 와 중복 호출해도 안전.
    """
    from app.models.organization import Store

    scope_ids = await empid_scope_store_ids(db, store_id)
    if len(scope_ids) > 1:
        # 그룹 공유 — (store_id, empid) 인덱스는 매장이 다르면 못 막으므로 트랜잭션 락으로 직렬화.
        group_id = (
            await db.execute(select(Store.group_id).where(Store.id == store_id))
        ).scalar_one()
        await db.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(str(group_id), 0)))
        )


async def next_empid(db: AsyncSession, store_id: UUID) -> int:
    """채번 스코프 안에서 다음 empid — max(MAX, floor-1)+1. 휴면·폐점 번호도 건너뜀.

    그룹 공유 스코프면 advisory lock(그룹 키)으로 동시 채번을 직렬화한다.
    """
    scope_ids = await empid_scope_store_ids(db, store_id)
    if len(scope_ids) > 1:
        await lock_empid_scope(db, store_id)
    floor = await _empid_floor(db, store_id)
    current_max = (
        await db.execute(
            select(func.coalesce(func.max(OrgMemberStore.empid), 0)).where(
                OrgMemberStore.store_id.in_(scope_ids)
            )
        )
    ).scalar() or 0
    return max(current_max, floor - 1) + 1


async def duplicate_empids_in_scope(db: AsyncSession, store_ids: list[UUID]) -> list[dict[str, int]]:
    """스코프(매장 집합) 안에서 중복 사용 중인 empid 목록 — [{empid, count}].

    그룹 편성/모드 전환 경고용. 기존 매장들은 각자 1..N 백필 상태라 그룹 공유로 묶는
    순간 중복이 생긴다 — 자동 재번호는 하지 않고(정책 A) 경고만, 해소는 EMPID 임포트에서.
    """
    if len(store_ids) < 2:
        return []
    rows = (
        await db.execute(
            select(OrgMemberStore.empid, func.count().label("cnt"))
            .where(OrgMemberStore.store_id.in_(store_ids), OrgMemberStore.empid.isnot(None))
            .group_by(OrgMemberStore.empid)
            .having(func.count() > 1)
            .order_by(OrgMemberStore.empid)
        )
    ).all()
    return [{"empid": r.empid, "count": r.cnt} for r in rows]


async def _org_member_id_for_store(db: AsyncSession, user_id: UUID, store_id: UUID) -> UUID | None:
    """user 가 그 store 의 org 에서 갖는 org_member id (없으면 None = legacy)."""
    from app.models.organization import Store

    org_id = (
        await db.execute(select(Store.organization_id).where(Store.id == store_id))
    ).scalar_one_or_none()
    if org_id is None:
        return None
    return (
        await db.execute(
            select(OrgMember.id).where(
                OrgMember.user_id == user_id, OrgMember.organization_id == org_id
            )
        )
    ).scalar_one_or_none()


async def ensure_member_store(
    db: AsyncSession,
    user_id: UUID,
    store_id: UUID,
    *,
    is_manager: bool = False,
    is_work_assignment: bool = True,
) -> None:
    """매장 배정 시 org_member_stores 행을 empid 부여하며 보장. 이미 있으면 속성만 갱신.

    (전환기: legacy user_stores 와 병행. org_member 없는 legacy 계정은 skip.)
    """
    member_id = await _org_member_id_for_store(db, user_id, store_id)
    if member_id is None:
        return
    existing = (
        await db.execute(
            select(OrgMemberStore).where(
                OrgMemberStore.org_member_id == member_id,
                OrgMemberStore.store_id == store_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.is_manager = is_manager
        existing.is_work_assignment = is_work_assignment
        return
    db.add(
        OrgMemberStore(
            org_member_id=member_id,
            store_id=store_id,
            is_manager=is_manager,
            is_work_assignment=is_work_assignment,
            empid=await next_empid(db, store_id),
        )
    )


async def remove_member_store(db: AsyncSession, user_id: UUID, store_id: UUID) -> None:
    """매장 배정 해제 — 정책 A: 행을 삭제하지 않고 휴면(플래그 false)으로 두어 empid 보존.

    나중에 재배정하면 ensure_member_store 가 이 행을 재사용 → 같은 empid 로 복귀.
    """
    member_id = await _org_member_id_for_store(db, user_id, store_id)
    if member_id is None:
        return
    row = (
        await db.execute(
            select(OrgMemberStore).where(
                OrgMemberStore.org_member_id == member_id,
                OrgMemberStore.store_id == store_id,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        row.is_work_assignment = False
        row.is_manager = False


async def reconcile_member_stores(
    db: AsyncSession, user_id: UUID, targets: list[dict]
) -> None:
    """sync 용 — targets(=[{store_id, is_manager, is_work_assignment}])에 org_member_stores 를 맞춘다.

    없는 매장은 empid 부여하며 추가, 목록 밖 매장은 삭제.
    """
    target_ids = {t["store_id"] for t in targets}
    # 현재 이 user 의 org_member_stores (모든 org 소속의 매장) 중 관련 매장만 처리
    for t in targets:
        await ensure_member_store(
            db, user_id, t["store_id"],
            is_manager=bool(t.get("is_manager")),
            is_work_assignment=bool(t.get("is_work_assignment", True)),
        )
    # 목록에서 빠진 매장 삭제 — user 의 모든 org_member 를 거쳐 org_member_stores 조회
    rows = (
        await db.execute(
            select(OrgMemberStore.store_id, OrgMemberStore.org_member_id)
            .join(OrgMember, OrgMember.id == OrgMemberStore.org_member_id)
            .where(OrgMember.user_id == user_id)
        )
    ).all()
    for store_id, _member_id in rows:
        if store_id not in target_ids:
            await remove_member_store(db, user_id, store_id)
