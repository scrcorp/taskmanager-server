"""관리자 알림 라우터 — 알림 관리 API.

Admin Notification Router — API endpoints for notification management.
Provides list, unread count, mark read, and mark all read operations.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.common import MessageResponse, NotificationResponse, PaginatedResponse
from app.services.notification_service import notification_service

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_notifications(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("dashboard:read"))],
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """관리자의 알림 목록을 조회합니다.

    List notifications for the admin user.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)
        page: 페이지 번호 (Page number)
        per_page: 페이지당 항목 수 (Items per page)

    Returns:
        dict: 페이지네이션된 알림 목록 (Paginated notification list)
    """
    notifications, total = await notification_service.list_notifications(
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
        for n in notifications
    ]

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/unread-count")
async def get_unread_count(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("dashboard:read"))],
) -> dict:
    """관리자의 읽지 않은 알림 수를 조회합니다.

    Get the count of unread notifications for the admin user.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 읽지 않은 알림 수 (Unread notification count)
    """
    count: int = await notification_service.get_unread_count(db, user_id=current_user.id)
    return {"unread_count": count}


@router.patch("/{notification_id}/read", response_model=MessageResponse)
async def mark_read(
    notification_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("dashboard:read"))],
) -> dict:
    """단일 알림을 읽음 처리합니다.

    Mark a single notification as read.

    Args:
        notification_id: 알림 UUID 문자열 (Notification UUID string)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 처리 결과 메시지 (Result message)
    """
    success: bool = await notification_service.mark_read(
        db,
        notification_id=notification_id,
        user_id=current_user.id,
    )
    await db.commit()

    if not success:
        from app.utils.exceptions import NotFoundError

        raise NotFoundError("알림을 찾을 수 없습니다 (Notification not found)")

    return {"message": "알림이 읽음 처리되었습니다 (Notification marked as read)"}


@router.patch("/read-all", response_model=MessageResponse)
async def mark_all_read(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("dashboard:read"))],
) -> dict:
    """모든 읽지 않은 알림을 읽음 처리합니다.

    Mark all unread notifications as read.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 처리 결과 메시지 (Result message with count)
    """
    count: int = await notification_service.mark_all_read(db, user_id=current_user.id)
    await db.commit()

    return {"message": f"{count}개의 알림이 읽음 처리되었습니다 ({count} notifications marked as read)"}
