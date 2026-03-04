from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.daily_report import (
    DailyReportTemplateCreate,
    DailyReportTemplateResponse,
    DailyReportTemplateSectionResponse,
    DailyReportTemplateUpdate,
)
from app.services.daily_report_service import daily_report_service

router: APIRouter = APIRouter()


def _build_template_response(template) -> dict:
    return {
        "id": str(template.id),
        "organization_id": str(template.organization_id) if template.organization_id else None,
        "store_id": str(template.store_id) if template.store_id else None,
        "name": template.name,
        "is_default": template.is_default,
        "is_active": template.is_active,
        "created_at": template.created_at,
        "sections": [
            {
                "id": str(s.id),
                "title": s.title,
                "description": s.description,
                "sort_order": s.sort_order,
                "is_required": s.is_required,
            }
            for s in template.sections
        ],
    }


@router.get("", response_model=list[DailyReportTemplateResponse])
async def list_templates(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:read"))],
    store_id: Annotated[str | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> list[dict]:
    templates = await daily_report_service.list_templates(
        db,
        organization_id=current_user.organization_id,
        store_id=UUID(store_id) if store_id else None,
        is_active=is_active,
    )
    return [_build_template_response(t) for t in templates]


@router.get("/{template_id}", response_model=DailyReportTemplateResponse)
async def get_template(
    template_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:read"))],
) -> dict:
    template = await daily_report_service.get_template_detail(
        db, template_id, current_user.organization_id
    )
    return _build_template_response(template)


@router.post("", response_model=DailyReportTemplateResponse, status_code=201)
async def create_template(
    data: DailyReportTemplateCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:create"))],
) -> dict:
    template = await daily_report_service.create_template(
        db, current_user.organization_id, data
    )
    await db.commit()
    return _build_template_response(template)


@router.put("/{template_id}", response_model=DailyReportTemplateResponse)
async def update_template(
    template_id: UUID,
    data: DailyReportTemplateUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:update"))],
) -> dict:
    template = await daily_report_service.update_template(
        db, template_id, current_user.organization_id, data
    )
    await db.commit()
    return _build_template_response(template)


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:delete"))],
) -> None:
    await daily_report_service.delete_template(
        db, template_id, current_user.organization_id
    )
    await db.commit()
