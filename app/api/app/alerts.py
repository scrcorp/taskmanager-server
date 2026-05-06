"""앱 알림 라우터 — 사용자용 알림 API.

App Alert Router — API endpoints for user's alert management.
Provides list, unread count, mark read, and mark all read operations.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.common import MessageResponse, AlertResponse, PaginatedResponse
from app.services.alert_service import alert_service

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_my_alerts(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """내 알림 목록을 조회합니다.

    List alerts for the current user.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)
        page: 페이지 번호 (Page number)
        per_page: 페이지당 항목 수 (Items per page)

    Returns:
        dict: 페이지네이션된 알림 목록 (Paginated alert list)
    """
    alerts, total = await alert_service.list_alerts(
        db,
        user_id=current_user.id,
        page=page,
        per_page=per_page,
    )

    items: list[dict] = [
        {
            "id": str(n.id),
            "type": n.type,
            "message": n.message,
            "reference_type": n.reference_type,
            "reference_id": str(n.reference_id) if n.reference_id else None,
            "is_read": n.is_read,
            "created_at": n.created_at,
        }
        for n in alerts
    ]

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/unread-count")
async def get_my_unread_count(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """내 읽지 않은 알림 수를 조회합니다.

    Get the count of unread alerts for the current user.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)

    Returns:
        dict: 읽지 않은 알림 수 (Unread alert count)
    """
    count: int = await alert_service.get_unread_count(db, user_id=current_user.id)
    return {"unread_count": count}


@router.patch("/{alert_id}/read", response_model=MessageResponse)
async def mark_my_alert_read(
    alert_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """단일 알림을 읽음 처리합니다.

    Mark a single alert as read.

    Args:
        alert_id: 알림 UUID (Alert UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)

    Returns:
        dict: 처리 결과 메시지 (Result message)
    """
    success: bool = await alert_service.mark_read(
        db,
        alert_id=alert_id,
        user_id=current_user.id,
    )

    if not success:
        from app.utils.exceptions import NotFoundError

        raise NotFoundError("Alert not found")

    return {"message": "Alert marked as read"}


@router.patch("/read-all", response_model=MessageResponse)
async def mark_all_my_alerts_read(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """모든 읽지 않은 알림을 읽음 처리합니다.

    Mark all unread alerts as read.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)

    Returns:
        dict: 처리 결과 메시지 (Result message with count)
    """
    count: int = await alert_service.mark_all_read(db, user_id=current_user.id)

    return {"message": f"{count} alerts marked as read"}
