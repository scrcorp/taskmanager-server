"""org 번호(crewid) / 매장 번호(empid) 채번 — 단일 게이트웨이.

crewid = org 안에서 1부터 MAX+1.

empid = **커서 기반**이다. 그룹/매장이 "다음 발급 번호"(next_empid)를 상태로 들고,
발급은 그 값에서 시작한다. 예전처럼 MAX(empid)+1 로 계산하지 않는다 —
예외 번호(본사 이관 등 대역 밖 번호)가 하나만 섞여도 순번 전체가 그리로 끌려가고,
번호를 가장 많이 가진 매장이 그룹을 떠나면 다음 사람이 이미 쓴 번호를 다시 받는다.

채번 스코프(scope): 매장이 그룹(store_groups)에 속하고 numbering_mode="group" 이면
그룹 내 전체 매장(폐점 포함 — 휴면·폐점 번호도 점유), 그 외에는 해당 매장 하나.
커서 보유 주체도 같은 규칙을 따른다 — 그룹 공유면 store_groups.next_empid,
그 외에는 stores.next_empid.

번호대(floor): store.number_range_start > group.number_range_start > 1 순 폴백.
커서 도입 후 floor 는 **커서 최초 초기화와 재계산에만** 쓰인다. 발급은 커서를 읽을 뿐이다.

불변식(계약 문서 INV-1~9 요약):
    - 채번에 MAX(empid) 를 쓰지 않는다. 예외는 커서 재계산과 최초 백필뿐이고,
      그때도 empid_kind='sequence' 로 한정한다.
    - 커서는 전진만 한다. 낮추는 것은 운영자의 명시적 수동 조정만 허용(사유 + 이력).
    - 발급된 empid 는 스코프 안에서 유일하다 — 커서가 점유 번호를 가리키면 건너뛴다.
    - 배정 해제(휴면)는 번호를 반납하지 않고 커서도 건드리지 않는다.
    - 수동 기입은 커서를 전진시키지 않는다.
    - 채번은 이 모듈만 통과한다. 새 채번 경로를 만들지 않는다.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org_member import EMPID_KIND_EXCEPTION, EMPID_KIND_SEQUENCE, OrgMember, OrgMemberStore

# 정책 A: 번호는 한 번 부여되면 고정. 배정 해제해도 행은 삭제하지 않고 휴면(플래그 false)으로
# 보존 → 재배정 시 같은 행/같은 empid 재사용. 휴면 행도 번호를 점유하므로 신규는 커서에서 새 번호.
# (껐다 켰다 해도 empid 불변, 무한히 안 올라감.)

# 커서 보유 주체 구분 — API 응답의 numbering.scope 와 같은 값이다(계약 §3-1).
EMPID_SCOPE_GROUP = "group"
EMPID_SCOPE_STORE = "store"


async def next_crewid(db: AsyncSession, organization_id: UUID) -> int:
    """org 안에서 다음 crewid — MAX+1 (1부터). 휴면 포함 사용 중 번호는 건너뜀.

    crewid 는 예외 번호 개념이 없고 org 단위 단일 시퀀스라 커서를 두지 않았다.
    """
    return (
        await db.execute(
            select(func.coalesce(func.max(OrgMember.crewid), 0) + 1).where(
                OrgMember.organization_id == organization_id
            )
        )
    ).scalar() or 1


# ---------------------------------------------------------------------------
# 스코프 판정 — 채번 대상 매장 집합 + 커서를 들고 있는 주체
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmpidCursorScope:
    """채번 스코프 1건 — 커서 주체와 번호를 점유하는 매장 집합.

    scope/scope_id 는 콘솔이 "어느 주체를 수정해야 하는지" 알기 위해 그대로 응답에 실린다.
    """

    scope: str          # EMPID_SCOPE_GROUP | EMPID_SCOPE_STORE
    scope_id: UUID      # 커서를 보유한 주체 id (그룹 id 또는 매장 id)
    store_ids: list[UUID]

    @property
    def is_group(self) -> bool:
        return self.scope == EMPID_SCOPE_GROUP


async def empid_cursor_scope(db: AsyncSession, store_id: UUID) -> EmpidCursorScope:
    """매장 기준 채번 스코프 판정.

    그룹 소속 + numbering_mode="group" → 그룹 커서 + 그룹 내 전체 매장(폐점 포함).
    그 외(미그룹 or mode="store") → 매장 커서 + 자기 자신만.
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
        return EmpidCursorScope(EMPID_SCOPE_STORE, store_id, [store_id])
    ids = (
        await db.execute(select(Store.id).where(Store.group_id == row.group_id))
    ).scalars().all()
    return EmpidCursorScope(EMPID_SCOPE_GROUP, row.group_id, list(ids) or [store_id])


async def group_cursor_scope(db: AsyncSession, group_id: UUID) -> EmpidCursorScope:
    """그룹 기준 스코프 — 그룹 설정 화면(커서 조회/조정)이 매장 없이 들어오는 경로.

    numbering_mode 와 무관하게 커서 주체는 그룹이다(Per-store 그룹의 그룹 커서는
    Shared 로 전환하기 전까지 쉬고 있을 뿐, 값 자체는 유지·조정 대상이다).
    """
    from app.models.organization import Store

    ids = (
        await db.execute(select(Store.id).where(Store.group_id == group_id))
    ).scalars().all()
    return EmpidCursorScope(EMPID_SCOPE_GROUP, group_id, list(ids))


async def empid_scope_store_ids(db: AsyncSession, store_id: UUID) -> list[UUID]:
    """empid 채번 스코프의 매장 id 목록 (기존 호출부 호환 래퍼)."""
    return (await empid_cursor_scope(db, store_id)).store_ids


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


async def _group_floor(db: AsyncSession, group_id: UUID) -> int:
    """그룹 커서의 번호대 시작값 — 그룹 number_range_start > 1."""
    from app.models.organization import StoreGroup

    return (
        await db.execute(select(StoreGroup.number_range_start).where(StoreGroup.id == group_id))
    ).scalar_one_or_none() or 1


async def _scope_floor(db: AsyncSession, scope: EmpidCursorScope) -> int:
    """스코프의 floor — 그룹 커서면 그룹값, 매장 커서면 매장 폴백 규칙."""
    if scope.is_group:
        return await _group_floor(db, scope.scope_id)
    return await _empid_floor(db, scope.scope_id)


# ---------------------------------------------------------------------------
# 커서 읽기/쓰기
# ---------------------------------------------------------------------------


async def _cursor_holder(db: AsyncSession, scope: EmpidCursorScope, *, for_update: bool = False):
    """커서를 들고 있는 ORM 행(StoreGroup | Store) 반환.

    for_update=True 면 SELECT ... FOR UPDATE 로 그 행을 잠근다. **커서 UPDATE 가
    직렬화 지점**이므로(계약 RULE-A) 발급 경로는 반드시 잠그고 읽는다. 이걸로
    단일 매장 스코프도 처음으로 락 보호를 받는다(예전엔 unique 충돌에만 의존했다).
    """
    from app.models.organization import Store, StoreGroup

    model = StoreGroup if scope.is_group else Store
    return await db.get(model, scope.scope_id, with_for_update=for_update or None)


async def _used_empids_at_or_above(
    db: AsyncSession, store_ids: list[UUID], start: int
) -> set[int]:
    """스코프 안에서 start 이상으로 이미 점유된 empid 집합 (건너뛰기 판정용).

    커서는 전진만 하므로 start 미만은 볼 필요가 없다. empid_changes(과거 번호)도 보지 않는다.
    """
    rows = (
        await db.execute(
            select(OrgMemberStore.empid).where(
                OrgMemberStore.store_id.in_(store_ids),
                OrgMemberStore.empid.isnot(None),
                OrgMemberStore.empid >= start,
            )
        )
    ).scalars().all()
    return set(rows)


async def lock_empid_scope(db: AsyncSession, store_id: UUID) -> None:
    """그룹 공유 스코프면 그룹 키 advisory lock 선취 (트랜잭션 종료 시 자동 해제).

    단일 매장 스코프는 no-op — 발급 경로가 매장 행을 FOR UPDATE 로 잠그고,
    (store_id, empid) partial unique 가 2차 방어.
    같은 트랜잭션 내 재획득은 무해(재진입)라 next_empid 와 중복 호출해도 안전.
    """
    scope = await empid_cursor_scope(db, store_id)
    if scope.is_group:
        await _lock_group(db, scope.scope_id)


async def _lock_group(db: AsyncSession, group_id: UUID) -> None:
    """그룹 공유 — (store_id, empid) 인덱스는 매장이 다르면 못 막으므로 트랜잭션 락으로 직렬화."""
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(group_id), 0)))
    )


async def next_empid(db: AsyncSession, store_id: UUID) -> int:
    """채번 스코프의 커서에서 다음 empid 를 발급한다 (계약 RULE-A).

        n = 커서 값 → 스코프 안에서 점유 중이면 +1 하며 건너뜀 → 커서 = n + 1

    MAX(empid) 는 쓰지 않는다(INV-1). 커서가 NULL 인 주체(도입 후 새로 만든 매장/그룹)는
    **floor 로 최초 1회 초기화**한다 — MAX 폴백이 아니다. 초기값이 이미 쓰인 번호를
    가리켜도 건너뛰기가 유일성을 지킨다.

    동시성: 그룹 공유 스코프는 advisory lock(그룹 키), 모든 스코프는 커서 행 FOR UPDATE.
    """
    scope = await empid_cursor_scope(db, store_id)
    if scope.is_group:
        await _lock_group(db, scope.scope_id)
    holder = await _cursor_holder(db, scope, for_update=True)
    n = holder.next_empid if holder is not None else None
    if n is None:
        # 커서 최초 초기화 — 백필 이후 새로 만들어진 주체. floor 에서 시작한다.
        n = await _scope_floor(db, scope)
    used = await _used_empids_at_or_above(db, scope.store_ids, n)
    while n in used:
        n += 1
    if holder is not None:
        holder.next_empid = n + 1  # 전진만 (INV-2)
    return n


async def promote_group_cursor(
    db: AsyncSession,
    *,
    group_id: UUID,
    store_id: UUID,
    changed_by: UUID | None = None,
    reason: str | None = None,
) -> int | None:
    """매장이 그룹에 편입될 때 그룹 커서를 승격한다 (계약 RULE-B).

        group.next_empid = max(group.next_empid, store.next_empid)

    **커서끼리 비교한다.** MAX(empid)+1 로 하면 그 매장의 예외 번호(대역 밖 수동 번호)가
    그룹 순번을 통째로 밀어올린다. 편입되는 매장이 이미 쓴 번호는 승격 대신
    발급 시 건너뛰기가 처리한다.

    호출부 배선(그룹 편성 변경 지점)은 store_service 담당. 여기서는 함수만 제공한다.
    올린 뒤의 그룹 커서를 반환(주체가 없으면 None).
    """
    from app.models.organization import Store, StoreGroup

    group = await db.get(StoreGroup, group_id, with_for_update=True)
    store = await db.get(Store, store_id)
    if group is None or store is None:
        return None
    previous = group.next_empid
    candidates = [v for v in (previous, store.next_empid) if v is not None]
    if not candidates:
        return previous
    promoted = max(candidates)
    if previous is not None and promoted == previous:
        return previous  # 이미 그룹이 앞서 있다 — 변화 없음
    group.next_empid = promoted
    await _log_cursor_change(
        db,
        organization_id=group.organization_id,
        store_id=store_id,
        old=previous,
        new=promoted,
        reason=reason,
        changed_by=changed_by,
    )
    return promoted


# ---------------------------------------------------------------------------
# 커서 상태 / 재계산 / 수동 조정 (RULE-C · RULE-E) — API 는 S3 가 얹는다
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmpidCursorState:
    """계약 §3-1 numbering 객체의 내용물 — 판정은 전부 여기(서버)서 끝난다(INV-8)."""

    next_empid: int | None   # 현재 커서 (미초기화면 None)
    recommended: int         # RULE-C 결과
    exception_count: int     # 재계산에서 제외된 예외 건수
    sequence_count: int      # 순번으로 분류된 번호 보유 건수
    mismatch: bool           # RULE-E — 커서가 순번 MAX 를 따라가지 못함
    scope: str
    scope_id: UUID

    def as_dict(self) -> dict:
        """응답에 그대로 실을 수 있는 dict (계약 §3-1 키 이름 고정)."""
        return {
            "next_empid": self.next_empid,
            "recommended": self.recommended,
            "exception_count": self.exception_count,
            "sequence_count": self.sequence_count,
            "mismatch": self.mismatch,
            "scope": self.scope,
            "scope_id": str(self.scope_id),
        }


async def empid_cursor_state(
    db: AsyncSession, *, store_id: UUID | None = None, group_id: UUID | None = None
) -> EmpidCursorState:
    """스코프의 커서 현황 + 권장값 + 분류 카운트 (RULE-C 계산 · RULE-E 판정).

    store_id 또는 group_id 중 하나를 준다. 적용은 하지 않는다(미리보기).
    """
    scope = (
        await group_cursor_scope(db, group_id)
        if group_id is not None
        else await empid_cursor_scope(db, store_id)  # type: ignore[arg-type]
    )
    holder = await _cursor_holder(db, scope)
    current = holder.next_empid if holder is not None else None

    counts = {EMPID_KIND_SEQUENCE: 0, EMPID_KIND_EXCEPTION: 0}
    base = 0
    if scope.store_ids:
        rows = (
            await db.execute(
                select(
                    OrgMemberStore.empid_kind,
                    func.count().label("cnt"),
                    func.max(OrgMemberStore.empid).label("mx"),
                )
                .where(
                    OrgMemberStore.store_id.in_(scope.store_ids),
                    OrgMemberStore.empid.isnot(None),
                )
                .group_by(OrgMemberStore.empid_kind)
            )
        ).all()
        for r in rows:
            counts[r.empid_kind] = r.cnt
            if r.empid_kind == EMPID_KIND_SEQUENCE:
                # 재계산은 sequence 한정 MAX — INV-1 의 명시적 예외.
                base = r.mx or 0

    floor = await _scope_floor(db, scope)
    recommended = max(base + 1, floor)
    return EmpidCursorState(
        next_empid=current,
        recommended=recommended,
        exception_count=counts[EMPID_KIND_EXCEPTION],
        sequence_count=counts[EMPID_KIND_SEQUENCE],
        mismatch=current is not None and current < recommended,
        scope=scope.scope,
        scope_id=scope.scope_id,
    )


async def set_empid_cursor(
    db: AsyncSession,
    *,
    scope: EmpidCursorScope,
    value: int,
    reason: str,
    changed_by: UUID | None = None,
) -> int | None:
    """커서를 지정값으로 설정한다 — 수동 조정·재계산 적용 공용. 이전 값을 반환.

    낮추는 것도 허용한다(INV-2 의 유일한 예외 = 운영자 명시 조작). 사유는 필수이고
    empid_changes 에 source='cursor' 로 남는다. 사유 누락/값 검증(ERR-*) 은 API 층 담당.
    """
    holder = await _cursor_holder(db, scope, for_update=True)
    if holder is None:
        return None
    previous = holder.next_empid
    if previous == value:
        return previous
    holder.next_empid = value
    await _log_cursor_change(
        db,
        organization_id=holder.organization_id,
        store_id=None if scope.is_group else scope.scope_id,
        old=previous,
        new=value,
        reason=reason,
        changed_by=changed_by,
    )
    return previous


async def recalculate_empid_cursor(
    db: AsyncSession,
    *,
    store_id: UUID | None = None,
    group_id: UUID | None = None,
    apply: bool = False,
    reason: str | None = None,
    changed_by: UUID | None = None,
) -> tuple[EmpidCursorState, int | None]:
    """커서 재계산 (RULE-C). (재계산 후 상태, 이전 커서) 반환.

        base = MAX(empid) WHERE empid_kind='sequence' AND empid IS NOT NULL   # 스코프 안
        new  = max(base + 1, floor)

    apply=False 면 미리보기만 — 커서를 건드리지 않는다. apply=True 면 사유 필수(호출부 검증).
    재계산 결과가 현재 커서보다 낮을 수 있고(예외 번호가 커서를 밀어올려 둔 경우),
    그때도 적용은 허용한다 — 경고는 API 가 previous/recommended 비교로 만든다.
    """
    state = await empid_cursor_state(db, store_id=store_id, group_id=group_id)
    if not apply:
        return state, state.next_empid
    scope = (
        await group_cursor_scope(db, state.scope_id)
        if state.scope == EMPID_SCOPE_GROUP
        else await empid_cursor_scope(db, state.scope_id)
    )
    previous = await set_empid_cursor(
        db, scope=scope, value=state.recommended,
        reason=reason or "cursor recalculated", changed_by=changed_by,
    )
    applied = EmpidCursorState(
        next_empid=state.recommended,
        recommended=state.recommended,
        exception_count=state.exception_count,
        sequence_count=state.sequence_count,
        mismatch=False,
        scope=state.scope,
        scope_id=state.scope_id,
    )
    return applied, previous


async def _log_cursor_change(
    db: AsyncSession,
    *,
    organization_id: UUID,
    store_id: UUID | None,
    old: int | None,
    new: int | None,
    reason: str | None,
    changed_by: UUID | None,
) -> None:
    """커서 자체 변경 이력 — old/new 에 사람 번호가 아니라 **커서 값**을 담는다(계약 §1-3)."""
    from app.core.client_surface import current_channel
    from app.models.empid_change import EMPID_SOURCE_CURSOR, EmpidChange

    db.add(EmpidChange(
        organization_id=organization_id,
        store_id=store_id,
        store_name=None,
        user_id=None,
        person_name=None,
        old_empid=old, new_empid=new,
        reason=reason,
        source=EMPID_SOURCE_CURSOR, channel=current_channel(), changed_by=changed_by,
    ))


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


async def _log_auto_assign(
    db: AsyncSession, member_id: UUID, store_id: UUID, empid: int
) -> None:
    """매장 배정 자동 채번의 empid_changes 이력 — actor 없음(시스템 부수효과).

    가입/유령 claim/매장 sync 등 여러 경로가 이 채번을 타므로, 채널(contextvar)이
    실제 진입 경로(console/staff_app/...)를 대신 말해준다.
    """
    from app.core.client_surface import current_channel
    from app.models.empid_change import EMPID_SOURCE_AUTO, EmpidChange
    from app.models.organization import Store
    from app.models.user import User as UserModel

    row = (
        await db.execute(
            select(OrgMember.user_id, UserModel.full_name, Store.name)
            .join(UserModel, UserModel.id == OrgMember.user_id)
            .join(Store, Store.id == store_id)
            .where(OrgMember.id == member_id)
        )
    ).first()
    db.add(EmpidChange(
        organization_id=(
            await db.scalar(select(Store.organization_id).where(Store.id == store_id))
        ),
        store_id=store_id,
        store_name=row.name if row else None,
        user_id=row.user_id if row else None,
        person_name=row.full_name if row else None,
        old_empid=None, new_empid=empid,
        source=EMPID_SOURCE_AUTO, channel=current_channel(), changed_by=None,
    ))


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
    assigned = await next_empid(db, store_id)
    db.add(
        OrgMemberStore(
            org_member_id=member_id,
            store_id=store_id,
            is_manager=is_manager,
            is_work_assignment=is_work_assignment,
            empid=assigned,
            # 자동 채번은 언제나 순번. 경로로 예외를 추론하지 않는다(INV-6).
            empid_kind=EMPID_KIND_SEQUENCE,
        )
    )
    await _log_auto_assign(db, member_id, store_id, assigned)


async def remove_member_store(db: AsyncSession, user_id: UUID, store_id: UUID) -> None:
    """매장 배정 해제 — 정책 A: 행을 삭제하지 않고 휴면(플래그 false)으로 두어 empid 보존.

    나중에 재배정하면 ensure_member_store 가 이 행을 재사용 → 같은 empid 로 복귀.
    **커서는 건드리지 않는다**(INV-4) — 번호를 반납하지 않으므로 되돌릴 것도 없다.
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
