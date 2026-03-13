"""직원용 업무 역할 조회 라우터."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.models.user_store import UserStore
from app.schemas.schedule import WorkRoleResponse
from app.services.work_role_service import work_role_service

router: APIRouter = APIRouter()


@router.get("/work-roles", response_model=list[WorkRoleResponse])
async def list_my_work_roles(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    store_id: str | None = None,
) -> list[WorkRoleResponse]:
    """내 매장의 업무 역할 목록.

    store_id가 없으면 사용자 소속 전체 매장의 역할을 반환.
    """
    if store_id is not None:
        return await work_role_service.list_work_roles(
            db, UUID(store_id), current_user.organization_id,
        )
    # store_id 없으면 사용자 소속 전체 매장 역할 반환
    from sqlalchemy import select
    result = await db.execute(
        select(UserStore.store_id).where(UserStore.user_id == current_user.id)
    )
    store_ids = [row[0] for row in result.all()]
    all_roles: list[WorkRoleResponse] = []
    for sid in store_ids:
        roles = await work_role_service.list_work_roles(
            db, sid, current_user.organization_id,
        )
        all_roles.extend(roles)
    return all_roles
