"""앱 이슈 리포트 라우터 — 직원용 이슈 리포트 API.

App Issue Report Router — Employee-facing issue report endpoints.
Any authenticated user can create and view their own issue reports.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.common import PaginatedResponse
from app.schemas.issue_report import IssueReportCreate
from app.services.issue_report_service import issue_report_service

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_my_issue_reports(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """내 이슈 리포트 목록 조회."""
    reports, total = await issue_report_service.list_for_user(
        db, current_user.organization_id, current_user.id, page, per_page
    )
    items = [await issue_report_service.build_response(db, r) for r in reports]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/{report_id}")
async def get_my_issue_report(
    report_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """내 이슈 리포트 상세 조회."""
    report = await issue_report_service.get_detail(db, report_id, current_user.organization_id)
    return await issue_report_service.build_response(db, report)


@router.post("", status_code=201)
async def create_issue_report(
    data: IssueReportCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """이슈 리포트 생성."""
    report = await issue_report_service.create_report(
        db, current_user.organization_id, data, current_user.id
    )
    await db.commit()
    return await issue_report_service.build_response(db, report)
