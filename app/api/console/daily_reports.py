from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, get_accessible_store_ids, require_permission
from app.core.permissions import is_owner
from app.database import get_db
from app.models.user import User
from app.core.error_codes.reports import REPORT_NOT_VISIBLE
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
    accessible = await get_accessible_store_ids(db, current_user)
    parsed_store_id = UUID(store_id) if store_id else None
    if parsed_store_id is not None:
        await check_store_access(db, current_user, parsed_store_id)
    reports, total = await daily_report_service.list_reports(
        db,
        organization_id=current_user.organization_id,
        store_id=parsed_store_id,
        date_from=date.fromisoformat(date_from) if date_from else None,
        date_to=date.fromisoformat(date_to) if date_to else None,
        period=period,
        status=status,
        page=page,
        per_page=per_page,
        accessible_store_ids=accessible,
        # legacy 라우트(콘솔 UI 미사용). 통합 /reports 와 같은 원칙으로 최소 권한:
        # Owner 외에는 본인 작성분만. 상세는 아래 get_report 에서 동일하게 차단.
        author_id=None if is_owner(current_user) else current_user.id,
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
    await check_store_access(db, current_user, report.store_id)
    if not is_owner(current_user) and report.author_id != current_user.id:
        raise REPORT_NOT_VISIBLE()
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
