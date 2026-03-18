import logging
from datetime import date, datetime
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.config import settings
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
from app.utils.email import send_email
from app.utils.email_templates import build_daily_report_email
from app.utils.pdf import build_daily_report_pdf
from app.utils.timezone import get_store_timezone

logger = logging.getLogger(__name__)

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
    items = await daily_report_service.build_responses_batch(db, reports)
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
    report = await daily_report_service.get_report(db, report.id, current_user.organization_id)
    return await daily_report_service.build_response(db, report, include_details=True)


@router.delete("/{report_id}", status_code=204)
async def delete_my_report(
    report_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:create"))],
) -> None:
    await daily_report_service.delete_report(
        db, report_id, current_user.organization_id, author_id=current_user.id
    )


@router.post("/{report_id}/submit", response_model=DailyReportResponse)
async def submit_report(
    report_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:update"))],
    background_tasks: BackgroundTasks,
) -> dict:
    report = await daily_report_service.submit_report(
        db, report_id, current_user.organization_id, current_user.id
    )
    report = await daily_report_service.get_report(db, report.id, current_user.organization_id)
    resp = await daily_report_service.build_response(db, report, include_details=True)

    # 이메일 알림 (background)
    if settings.REPORT_NOTIFICATION_EMAIL:
        store_tz_name = await get_store_timezone(db, report.store_id)
        store_tz = ZoneInfo(store_tz_name)

        # 제출 시각: 매장 타임존 기준 US 포맷
        submitted_at_fmt = ""
        if report.submitted_at:
            local_dt = report.submitted_at.astimezone(store_tz)
            submitted_at_fmt = local_dt.strftime("%b %d, %Y (%a) %I:%M %p")

        # 리포트 날짜: 요일 포함 US 포맷
        report_date_obj = datetime(
            report.report_date.year, report.report_date.month, report.report_date.day,
            tzinfo=store_tz,
        )
        report_date_fmt = report_date_obj.strftime("%b %d, %Y (%a)")

        email_kwargs = dict(
            store_name=resp.get("store_name", ""),
            report_date=report_date_fmt,
            period=report.period,
            author_name=resp.get("author_name", ""),
            submitted_at=submitted_at_fmt,
            sections=resp.get("sections", []),
        )
        subject, html = build_daily_report_email(**email_kwargs)
        pdf_filename, pdf_bytes = build_daily_report_pdf(**email_kwargs)
        background_tasks.add_task(
            send_email,
            to=settings.REPORT_NOTIFICATION_EMAIL,
            subject=subject,
            html=html,
            attachments=[(pdf_filename, pdf_bytes)],
        )

    return resp
