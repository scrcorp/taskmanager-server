"""콘솔 근무가능시간 라우터 — 매니저가 스태프 근무가능시간 조회/편집.

권한: availability:read / availability:manage. 편집 IDOR: 대상이 호출자 접근가능
매장과 최소 1개 공유(같은 org). Owner 는 전 org 접근(accessible=None).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    check_store_access,
    get_accessible_store_ids,
    require_permission,
)
from app.database import get_db
from app.models.user import User
from app.repositories.availability_repository import availability_repository
from app.repositories.user_repository import user_repository
from app.schemas.availability import (
    AvailabilityDetailOut,
    AvailabilityMemberOut,
    AvailabilityWeekUpdate,
    PresetCreate,
    PresetOut,
)
from app.services.availability_service import availability_service
from app.utils.exceptions import ForbiddenError, NotFoundError

router: APIRouter = APIRouter()


async def _load_target(db: AsyncSession, user_id: UUID) -> User:
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise NotFoundError("Staff not found")
    return user


async def _assert_can_access(db: AsyncSession, caller: User, target: User) -> None:
    """대상 스태프 접근 가드 — 같은 org + (Owner 전체 / 그 외 공유 매장)."""
    if target.organization_id != caller.organization_id:
        raise NotFoundError("Staff not found")  # cross-org 존재 은닉
    if caller.id == target.id:
        return
    accessible = await get_accessible_store_ids(db, caller)
    if accessible is None:  # Owner — 전 org 접근
        return
    target_store_ids = await user_repository.get_user_store_ids(db, target.id)
    if not (set(accessible) & set(target_store_ids)):
        raise ForbiddenError("You can only manage availability for staff in stores you can access")


@router.get("", response_model=list[AvailabilityMemberOut])
async def list_availability(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("availability:read"))],
    store_id: Annotated[UUID | None, Query(description="Filter to staff in this store")] = None,
) -> list[AvailabilityMemberOut]:
    org_id = current_user.organization_id
    if store_id is not None:
        await check_store_access(db, current_user, store_id)
        user_ids = await availability_repository.user_ids_in_store(db, store_id)
    else:
        accessible = await get_accessible_store_ids(db, current_user)
        if accessible is None:  # Owner — 전 org 스태프
            user_ids = list(
                (await db.execute(select(User.id).where(User.organization_id == org_id))).scalars().all()
            )
        elif not accessible:
            user_ids = []
        else:
            user_ids = await availability_repository.user_ids_in_stores(db, accessible)
    # dedupe, order-stable
    user_ids = list(dict.fromkeys(user_ids))
    return await availability_service.get_bulk(db, org_id, user_ids)


# ─────────────────────────── 프리셋 (기본 세팅) ───────────────────────────
@router.get("/presets", response_model=list[PresetOut])
async def list_presets(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("availability:read"))],
) -> list[PresetOut]:
    return await availability_service.list_presets(db, current_user.organization_id)


@router.post("/presets", response_model=PresetOut, status_code=status.HTTP_201_CREATED)
async def create_preset(
    data: PresetCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("availability:manage"))],
) -> PresetOut:
    return await availability_service.create_preset(
        db, current_user.organization_id, data, actor_id=current_user.id
    )


@router.delete("/presets/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_preset(
    preset_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("availability:manage"))],
) -> None:
    await availability_service.delete_preset(db, current_user.organization_id, preset_id)


@router.get("/staff/{user_id}", response_model=AvailabilityDetailOut)
async def get_staff_availability(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("availability:read"))],
) -> AvailabilityDetailOut:
    target = await _load_target(db, user_id)
    await _assert_can_access(db, current_user, target)
    member = await availability_service.get_member(
        db, current_user.organization_id, user_id, full_name=target.full_name
    )
    history = await availability_service.get_history(db, current_user.organization_id, user_id)
    return AvailabilityDetailOut(member=member, history=history)


@router.put("/staff/{user_id}", response_model=AvailabilityMemberOut)
async def save_staff_availability(
    user_id: UUID,
    data: AvailabilityWeekUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("availability:manage"))],
) -> AvailabilityMemberOut:
    target = await _load_target(db, user_id)
    await _assert_can_access(db, current_user, target)
    return await availability_service.save_week(
        db, current_user.organization_id, user_id, data.days,
        actor_id=current_user.id, source="console_manager",
    )
