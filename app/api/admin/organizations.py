"""관리자 조직 라우터 — 현재 조직 조회 및 수정.

Admin Organization Router — Retrieve and update the current organization.
Only accessible by owner-level users.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.organization import OrganizationResponse, OrganizationUpdate
from app.services.organization_service import organization_service

router: APIRouter = APIRouter()


@router.get("/me", response_model=OrganizationResponse)
async def get_current_organization(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> OrganizationResponse:
    """현재 조직 정보를 조회합니다.

    Retrieve the current organization's details (from JWT).
    """
    org_id: UUID = current_user.organization_id
    return await organization_service.get_current(db, org_id)


@router.put("/me", response_model=OrganizationResponse)
async def update_current_organization(
    data: OrganizationUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> OrganizationResponse:
    """현재 조직 정보를 수정합니다.

    Update the current organization's details.
    """
    org_id: UUID = current_user.organization_id
    result: OrganizationResponse = await organization_service.update_current(
        db, org_id, data
    )
    await db.commit()
    return result
