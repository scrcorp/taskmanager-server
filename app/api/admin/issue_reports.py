"""관리자 이슈 리포트 라우터 — 이슈 리포트 관리 API.

Admin Issue Report Router — CRUD endpoints for issue report management.
All admin roles (SV+) can view. GM+ can update status. Any authenticated user can create.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_gm, require_supervisor
from app.database import get_db
from app.models.user import User
from app.schemas.common import MessageResponse, PaginatedResponse
from app.schemas.issue_report import IssueReportCreate, IssueReportUpdate
from app.services.issue_report_service import issue_report_service

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_issue_reports(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
    status: str | None = Query(None),
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """이슈 리포트 목록 조회. SV+ 가능."""
    reports, total = await issue_report_service.list_reports(
        db, current_user.organization_id, status, page, per_page
    )
    items = [await issue_report_service.build_response(db, r) for r in reports]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/{report_id}")
async def get_issue_report(
    report_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """이슈 리포트 상세 조회."""
    report = await issue_report_service.get_detail(db, report_id, current_user.organization_id)
    return await issue_report_service.build_response(db, report)


@router.post("", status_code=201)
async def create_issue_report(
    data: IssueReportCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """이슈 리포트 생성. 전 역할 가능."""
    report = await issue_report_service.create_report(
        db, current_user.organization_id, data, current_user.id
    )
    await db.commit()
    return await issue_report_service.build_response(db, report)


@router.put("/{report_id}")
async def update_issue_report(
    report_id: UUID,
    data: IssueReportUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> dict:
    """이슈 리포트 수정. GM+ 가능."""
    report = await issue_report_service.update_report(
        db, report_id, current_user.organization_id, data, current_user.id
    )
    await db.commit()
    return await issue_report_service.build_response(db, report)


@router.delete("/{report_id}", response_model=MessageResponse)
async def delete_issue_report(
    report_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> dict:
    """이슈 리포트 삭제. GM+ 가능."""
    await issue_report_service.delete_report(db, report_id, current_user.organization_id)
    await db.commit()
    return {"message": "이슈 리포트가 삭제되었습니다 (Issue report deleted)"}
