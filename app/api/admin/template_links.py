"""관리자 체크리스트 템플릿 연결 라우터.

Admin Checklist Template Links Router — CRUD for cl_template_links.
Permission: Owner + GM.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.common import MessageResponse, TemplateLinkCreate, TemplateLinkResponse
from app.services.template_link_service import template_link_service

router: APIRouter = APIRouter()


@router.post(
    "/checklist-template-links",
    response_model=TemplateLinkResponse,
    status_code=201,
)
async def create_template_link(
    data: TemplateLinkCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:create"))],
) -> dict:
    """체크리스트 템플릿 연결을 생성합니다. Owner + GM."""
    link = await template_link_service.create_link(
        db,
        organization_id=current_user.organization_id,
        data=data,
    )
    await db.commit()
    return await template_link_service.build_response(db, link)


@router.get(
    "/checklist-template-links",
    response_model=list[TemplateLinkResponse],
)
async def list_template_links(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:read"))],
    store_id: Annotated[str | None, Query()] = None,
    template_id: Annotated[str | None, Query()] = None,
) -> list[dict]:
    """체크리스트 템플릿 연결 목록을 조회합니다. Owner + GM."""
    links = await template_link_service.list_links(
        db,
        organization_id=current_user.organization_id,
        store_id=UUID(store_id) if store_id else None,
        template_id=UUID(template_id) if template_id else None,
    )
    return [await template_link_service.build_response(db, link) for link in links]


@router.delete(
    "/checklist-template-links/{link_id}",
    response_model=MessageResponse,
)
async def delete_template_link(
    link_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:delete"))],
) -> dict:
    """체크리스트 템플릿 연결을 삭제합니다. Owner + GM."""
    await template_link_service.delete_link(
        db,
        link_id=link_id,
        organization_id=current_user.organization_id,
    )
    await db.commit()
    return {"message": "Template link deleted"}
