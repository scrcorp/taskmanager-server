"""관리자 대시보드 라우터 — 대시보드 집계 API.

Admin Dashboard Router — API endpoints for dashboard aggregation data.
Provides checklist completion rates, attendance summary, overtime summary,
and evaluation summary for the admin dashboard.

Permission: SV+ (all admin roles)
"""

from datetime import date
from io import BytesIO
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.services.dashboard_service import dashboard_service

router: APIRouter = APIRouter()


@router.get("/checklist-completion")
async def get_checklist_completion(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("dashboard:read"))],
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    store_id: Annotated[str | None, Query()] = None,
) -> dict:
    """체크리스트 완료율 조회 — 기간별 배정 완료 통계."""
    return await dashboard_service.get_checklist_completion(
        db,
        organization_id=current_user.organization_id,
        date_from=date_from,
        date_to=date_to,
        store_id=UUID(store_id) if store_id else None,
    )


@router.get("/attendance-summary")
async def get_attendance_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("dashboard:read"))],
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    store_id: Annotated[str | None, Query()] = None,
) -> dict:
    """근태 요약 조회 — 기간별 근태 통계."""
    return await dashboard_service.get_attendance_summary(
        db,
        organization_id=current_user.organization_id,
        date_from=date_from,
        date_to=date_to,
        store_id=UUID(store_id) if store_id else None,
    )


@router.get("/overtime-summary")
async def get_overtime_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("dashboard:read"))],
    week_date: Annotated[date | None, Query()] = None,
    store_id: Annotated[str | None, Query()] = None,
) -> dict:
    """초과근무 현황 요약 조회 — 주간 초과근무 통계."""
    return await dashboard_service.get_overtime_summary(
        db,
        organization_id=current_user.organization_id,
        week_date=week_date,
        store_id=UUID(store_id) if store_id else None,
    )


@router.get("/export")
async def export_dashboard(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("dashboard:read"))],
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    store_id: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    """대시보드 데이터를 Excel 파일로 내보냅니다. Owner + GM."""
    excel_bytes: bytes = await dashboard_service.export_excel(
        db,
        organization_id=current_user.organization_id,
        date_from=date_from,
        date_to=date_to,
        store_id=UUID(store_id) if store_id else None,
    )
    return StreamingResponse(
        BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=dashboard_export.xlsx"},
    )


@router.get("/evaluation-summary")
async def get_evaluation_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("dashboard:read"))],
) -> dict:
    """평가 요약 조회 — 전체 평가 통계."""
    return await dashboard_service.get_evaluation_summary(
        db,
        organization_id=current_user.organization_id,
    )
