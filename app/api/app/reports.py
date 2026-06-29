"""App (mobile) multi-type reports API.

작성자(staff/sv) 본인의 리포트 생성/수정/제출/삭제 + 템플릿 조회.
"""
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
from app.schemas.report import (
    ReportCommentCreate,
    ReportCreate,
    ReportResponse,
    ReportReviewRequest,
    ReportTemplateResponse,
    ReportUpdate,
)
from app.services.report_service import report_service
from app.utils.email import send_email
from pydantic import BaseModel


class IssueStatusTransition(BaseModel):
    status: str
from app.utils.email_templates import build_daily_report_email
from app.utils.pdf import build_daily_report_pdf
from app.utils.timezone import get_store_timezone

logger = logging.getLogger(__name__)
router: APIRouter = APIRouter()


@router.get("/template", response_model=ReportTemplateResponse)
async def get_template(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:read"))],
    type: Annotated[str, Query()] = "daily",
    store_id: Annotated[str | None, Query()] = None,
) -> dict:
    t = await report_service.get_template_for_use(
        db,
        type=type,
        organization_id=current_user.organization_id,
        store_id=UUID(store_id) if store_id else None,
    )
    return report_service.build_template_response(t)


@router.get("/report-types")
async def list_effective_report_types(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:read"))],
    store_id: Annotated[str | None, Query()] = None,
    active_only: Annotated[bool, Query()] = True,
) -> dict:
    """매장에 enabled 된 report type(period) 목록 — type selector 채우기용.

    active_only=True(default) → 활성 타입만. False → 비활성 포함 전체.
    store_id 없으면 org-default 기준.
    """
    items = await report_service.resolve_effective_types(
        db,
        organization_id=current_user.organization_id,
        store_id=UUID(store_id) if store_id else None,
    )
    if active_only:
        items = [i for i in items if i["is_active"]]
    return {"items": items}


@router.get("")
async def list_my_reports(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:read"))],
    type: Annotated[str | None, Query()] = None,
    store_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    period: Annotated[str | None, Query()] = None,
    show_all: Annotated[bool, Query()] = False,
    only_mine: Annotated[bool, Query()] = True,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    # daily는 작성자 본인 것만(only_mine 기본 True), issue는 visibility 기반(only_mine False)
    author_filter = current_user.id if (only_mine and type != "issue") else None
    reports, total = await report_service.list_reports(
        db,
        organization_id=current_user.organization_id,
        type=type,
        author_id=author_filter,
        store_id=UUID(store_id) if store_id else None,
        status=status,
        date_from=date.fromisoformat(date_from) if date_from else None,
        date_to=date.fromisoformat(date_to) if date_to else None,
        period=period,
        exclude_draft=False,
        page=page,
        per_page=per_page,
        viewer=current_user,
        show_all=show_all,
    )
    items = await report_service.build_responses_batch(db, reports)
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/{report_id}", response_model=ReportResponse)
async def get_my_report(
    report_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:read"))],
) -> dict:
    r = await report_service.get_report(db, report_id, current_user.organization_id)
    return await report_service.build_response(db, r, include_comments=True)


@router.post("", status_code=201)
async def create_report(
    data: ReportCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:create"))],
) -> dict:
    r = await report_service.create_report(
        db, current_user.organization_id, current_user.id, data
    )
    r = await report_service.get_report(db, r.id, current_user.organization_id)
    return await report_service.build_response(db, r, include_comments=True)


@router.put("/{report_id}", response_model=ReportResponse)
async def update_report(
    report_id: UUID,
    data: ReportUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:update"))],
) -> dict:
    r = await report_service.update_report(
        db, report_id, current_user.organization_id, current_user.id, data
    )
    r = await report_service.get_report(db, r.id, current_user.organization_id)
    return await report_service.build_response(db, r, include_comments=True)


@router.post("/{report_id}/transition", response_model=ReportResponse)
async def transition_issue_status(
    report_id: UUID,
    data: IssueStatusTransition,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:update"))],
) -> dict:
    r = await report_service.transition_issue_status(
        db, report_id, current_user.organization_id, current_user.id, data.status
    )
    r = await report_service.get_report(db, r.id, current_user.organization_id)
    return await report_service.build_response(db, r, include_comments=True)


@router.delete("/{report_id}", status_code=204)
async def delete_my_report(
    report_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:create"))],
) -> None:
    await report_service.delete_report(
        db, report_id, current_user.organization_id, author_id=current_user.id
    )


@router.post("/{report_id}/submit", response_model=ReportResponse)
async def submit_report(
    report_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:update"))],
    background_tasks: BackgroundTasks,
) -> dict:
    r = await report_service.submit_report(
        db, report_id, current_user.organization_id, current_user.id
    )
    r = await report_service.get_report(db, r.id, current_user.organization_id)
    resp = await report_service.build_response(db, r, include_comments=True)

    # daily 타입은 이메일 알림 + PDF (기존 동작 보존)
    if r.type == "daily" and settings.REPORT_NOTIFICATION_EMAIL:
        store_tz_name = await get_store_timezone(db, r.store_id)
        store_tz = ZoneInfo(store_tz_name)

        submitted_at_fmt = ""
        if r.submitted_at:
            local_dt = r.submitted_at.astimezone(store_tz)
            submitted_at_fmt = local_dt.strftime("%b %d, %Y (%a) %I:%M %p")

        report_date_fmt = ""
        if r.report_date:
            rd = datetime(r.report_date.year, r.report_date.month, r.report_date.day, tzinfo=store_tz)
            report_date_fmt = rd.strftime("%b %d, %Y (%a)")

        period = (r.payload or {}).get("period", "")
        sections = (r.payload or {}).get("sections", [])
        email_kwargs = dict(
            store_name=resp.get("store_name", ""),
            report_date=report_date_fmt,
            period=period,
            author_name=resp.get("author_name", ""),
            submitted_at=submitted_at_fmt,
            sections=sections,
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


@router.post("/{report_id}/review", response_model=ReportResponse)
async def review_report(
    report_id: UUID,
    data: ReportReviewRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:review"))],
) -> dict:
    """리포트 검토 완료 (SV+). submitted → reviewed + 선택 feedback 코멘트."""
    r = await report_service.review_report(
        db, report_id, current_user.organization_id, current_user.id, data.feedback
    )
    r = await report_service.get_report(db, r.id, current_user.organization_id)
    return await report_service.build_response(db, r, include_comments=True)


@router.post("/{report_id}/acknowledge", response_model=ReportResponse)
async def acknowledge_report(
    report_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:acknowledge"))],
) -> dict:
    """리포트 읽음 확인 (멱등)."""
    await report_service.acknowledge_report(
        db, report_id, current_user.organization_id, current_user.id
    )
    r = await report_service.get_report(db, report_id, current_user.organization_id)
    return await report_service.build_response(db, r, include_comments=True)


@router.post("/{report_id}/comments")
async def add_comment(
    report_id: UUID,
    data: ReportCommentCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:update"))],
) -> dict:
    c = await report_service.add_comment(
        db, report_id, current_user.organization_id, current_user.id, data
    )
    return {
        "id": str(c.id),
        "report_id": str(c.report_id),
        "user_id": str(c.user_id) if c.user_id else None,
        "content": c.content,
        "created_at": c.created_at,
    }
