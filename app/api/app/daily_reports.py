from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.daily_report import (
    DailyReportCommentCreate,
    DailyReportCreate,
    DailyReportResponse,
    DailyReportTemplateResponse,
    DailyReportUpdate,
)
from app.services.daily_report_service import daily_report_service

router: APIRouter = APIRouter()


@router.get("/template", response_model=DailyReportTemplateResponse)
async def get_template(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:read"))],
    store_id: Annotated[str | None, Query()] = None,
) -> dict:
    template = await daily_report_service.get_template(
        db,
        organization_id=current_user.organization_id,
        store_id=UUID(store_id) if store_id else None,
    )
    return await daily_report_service.build_template_response(template)


@router.get("")
async def list_my_reports(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:read"))],
    store_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    reports, total = await daily_report_service.list_reports(
        db,
        organization_id=current_user.organization_id,
        author_id=current_user.id,
        store_id=UUID(store_id) if store_id else None,
        status=status,
        exclude_draft=False,
        page=page,
        per_page=per_page,
    )
    items = [await daily_report_service.build_response(db, r) for r in reports]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/{report_id}", response_model=DailyReportResponse)
async def get_my_report(
    report_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:read"))],
) -> dict:
    report = await daily_report_service.get_report(db, report_id, current_user.organization_id)
    return await daily_report_service.build_response(db, report, include_details=True)


@router.post("", status_code=201)
async def create_report(
    data: DailyReportCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:create"))],
) -> dict:
    report = await daily_report_service.create_report(
        db, current_user.organization_id, current_user.id, data
    )
    await db.commit()
    # Re-fetch with details for response
    report = await daily_report_service.get_report(db, report.id, current_user.organization_id)
    return await daily_report_service.build_response(db, report, include_details=True)


@router.put("/{report_id}", response_model=DailyReportResponse)
async def update_report(
    report_id: UUID,
    data: DailyReportUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:update"))],
) -> dict:
    report = await daily_report_service.update_report(
        db, report_id, current_user.organization_id, current_user.id, data
    )
    await db.commit()
    report = await daily_report_service.get_report(db, report.id, current_user.organization_id)
    return await daily_report_service.build_response(db, report, include_details=True)


@router.post("/{report_id}/submit", response_model=DailyReportResponse)
async def submit_report(
    report_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:update"))],
) -> dict:
    report = await daily_report_service.submit_report(
        db, report_id, current_user.organization_id, current_user.id
    )
    await db.commit()
    report = await daily_report_service.get_report(db, report.id, current_user.organization_id)
    return await daily_report_service.build_response(db, report, include_details=True)
