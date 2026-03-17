from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.daily_report import DailyReportCommentCreate, DailyReportResponse
from app.services.daily_report_service import daily_report_service

router: APIRouter = APIRouter()


@router.get("")
async def list_reports(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:read"))],
    store_id: Annotated[str | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    period: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    reports, total = await daily_report_service.list_reports(
        db,
        organization_id=current_user.organization_id,
        store_id=UUID(store_id) if store_id else None,
        date_from=date.fromisoformat(date_from) if date_from else None,
        date_to=date.fromisoformat(date_to) if date_to else None,
        period=period,
        status=status,
        page=page,
        per_page=per_page,
    )
    items = await daily_report_service.build_responses_batch(db, reports)
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/{report_id}", response_model=DailyReportResponse)
async def get_report(
    report_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:read"))],
) -> dict:
    report = await daily_report_service.get_report(db, report_id, current_user.organization_id)
    return await daily_report_service.build_response(db, report, include_details=True)


@router.delete("/{report_id}", status_code=204)
async def delete_report(
    report_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:delete"))],
) -> None:
    await daily_report_service.delete_report(db, report_id, current_user.organization_id)


@router.post("/{report_id}/comments")
async def add_comment(
    report_id: UUID,
    data: DailyReportCommentCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("daily_reports:update"))],
) -> dict:
    comment = await daily_report_service.add_comment(
        db, report_id, current_user.organization_id, current_user.id, data
    )
    # Resolve user name for response
    user_result = await db.execute(sa_select(User.full_name).where(User.id == comment.user_id))
    user_name = user_result.scalar() or "Unknown"
    return {
        "id": str(comment.id),
        "report_id": str(comment.report_id),
        "user_id": str(comment.user_id),
        "user_name": user_name,
        "content": comment.content,
        "created_at": comment.created_at,
    }
