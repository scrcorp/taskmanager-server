"""Console multi-type reports API.

GET    /reports                       — list (type 필터 가능)
GET    /reports/{id}                  — detail
DELETE /reports/{id}                  — delete (admin)
POST   /reports/{id}/comments         — add comment

Reports 생성/수정/제출은 app 라우터에서 (작성자만 가능).
"""
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, get_accessible_store_ids, require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.report import (
    ReportCommentCreate,
    ReportCreate,
    ReportResponse,
    ReportReviewRequest,
    ReportUpdate,
)
from app.services.report_service import report_service
from pydantic import BaseModel

router: APIRouter = APIRouter()


class IssueStatusTransition(BaseModel):
    status: str  # open | in_progress | closed


@router.get("")
async def list_reports(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:read"))],
    type: Annotated[str | None, Query()] = None,
    store_id: Annotated[str | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    period: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    show_all: Annotated[bool, Query()] = False,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    accessible = await get_accessible_store_ids(db, current_user)
    parsed_store_id = UUID(store_id) if store_id else None
    if parsed_store_id is not None:
        await check_store_access(db, current_user, parsed_store_id)
    reports, total = await report_service.list_reports(
        db,
        organization_id=current_user.organization_id,
        type=type,
        store_id=parsed_store_id,
        date_from=date.fromisoformat(date_from) if date_from else None,
        date_to=date.fromisoformat(date_to) if date_to else None,
        period=period,
        status=status,
        page=page,
        per_page=per_page,
        accessible_store_ids=accessible,
        viewer=current_user,
        show_all=show_all,
    )
    items = await report_service.build_responses_batch(db, reports)
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:read"))],
) -> dict:
    r = await report_service.get_report(db, report_id, current_user.organization_id)
    if r.store_id:
        await check_store_access(db, current_user, r.store_id)
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
    from app.core.permissions import is_gm_plus
    r = await report_service.update_report(
        db,
        report_id,
        current_user.organization_id,
        current_user.id,
        data,
        is_manager=is_gm_plus(current_user),
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


@router.post("/{report_id}/review", response_model=ReportResponse)
async def review_report(
    report_id: UUID,
    data: ReportReviewRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:review"))],
) -> dict:
    r = await report_service.get_report(db, report_id, current_user.organization_id)
    if r.store_id:
        await check_store_access(db, current_user, r.store_id)
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
    r = await report_service.get_report(db, report_id, current_user.organization_id)
    if r.store_id:
        await check_store_access(db, current_user, r.store_id)
    await report_service.acknowledge_report(
        db, report_id, current_user.organization_id, current_user.id
    )
    r = await report_service.get_report(db, report_id, current_user.organization_id)
    return await report_service.build_response(db, r, include_comments=True)


@router.delete("/{report_id}", status_code=204)
async def delete_report(
    report_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:delete"))],
) -> None:
    await report_service.delete_report(db, report_id, current_user.organization_id)


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
    u = await db.execute(sa_select(User.full_name).where(User.id == c.user_id))
    return {
        "id": str(c.id),
        "report_id": str(c.report_id),
        "user_id": str(c.user_id) if c.user_id else None,
        "user_name": u.scalar() or "Unknown",
        "content": c.content,
        "created_at": c.created_at,
    }
