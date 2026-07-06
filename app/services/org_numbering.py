"""org 번호(crewid) / 매장 번호(empid) 다음 순번 계산.

crewid = org 안에서 1부터, empid = 매장(store) 안에서 1부터. 부여 규칙 없이 단순 MAX+1.
동시성: MAX+1 은 경합 시 같은 번호가 날 수 있으나 partial unique 로 걸린다(트라이얼 단계 수용).
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


async def next_empid(db: AsyncSession, store_id: UUID) -> int:
    """매장 안에서 다음 empid — MAX+1 (1부터). 휴면 포함 사용 중 번호는 건너뜀."""
    return (
        await db.execute(
            select(func.coalesce(func.max(OrgMemberStore.empid), 0) + 1).where(
                OrgMemberStore.store_id == store_id
            )
        )
    ).scalar() or 1


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
