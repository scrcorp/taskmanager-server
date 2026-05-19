"""Console report templates API (multi-type)."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.report import (
    ReportTemplateCreate,
    ReportTemplateResponse,
    ReportTemplateUpdate,
)
from app.services.report_service import report_service

router: APIRouter = APIRouter()


@router.get("/lookup", response_model=ReportTemplateResponse)
async def lookup_template(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:read"))],
    type: Annotated[str, Query()] = "issue",
    store_id: Annotated[str | None, Query()] = None,
) -> dict:
    """매장에 적용될 effective template (store → org → system default fallback)."""
    t = await report_service.get_template_for_use(
        db,
        type=type,
        organization_id=current_user.organization_id,
        store_id=UUID(store_id) if store_id else None,
    )
    return report_service.build_template_response(t)


@router.get("")
async def list_templates(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:read"))],
    type: Annotated[str | None, Query()] = None,
    store_id: Annotated[str | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> dict:
    templates = await report_service.list_templates(
        db,
        organization_id=current_user.organization_id,
        type=type,
        store_id=UUID(store_id) if store_id else None,
        is_active=is_active,
    )
    return {"items": [report_service.build_template_response(t) for t in templates]}


@router.get("/{template_id}", response_model=ReportTemplateResponse)
async def get_template(
    template_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:read"))],
) -> dict:
    t = await report_service.get_template_detail(db, template_id, current_user.organization_id)
    return report_service.build_template_response(t)


@router.post("", status_code=201, response_model=ReportTemplateResponse)
async def create_template(
    data: ReportTemplateCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:create"))],
) -> dict:
    t = await report_service.create_template(db, current_user.organization_id, data)
    return report_service.build_template_response(t)


@router.put("/{template_id}", response_model=ReportTemplateResponse)
async def update_template(
    template_id: UUID,
    data: ReportTemplateUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:update"))],
) -> dict:
    t = await report_service.update_template(db, template_id, current_user.organization_id, data)
    return report_service.build_template_response(t)


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:delete"))],
) -> None:
    await report_service.delete_template(db, template_id, current_user.organization_id)
