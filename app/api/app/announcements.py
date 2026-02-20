"""앱 공지사항 라우터 — 사용자용 공지사항 조회 API.

App Announcement Router — API endpoints for user's announcement viewing.
Provides read-only access to org-wide and store-specific announcements.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.models.user_store import UserStore
from app.schemas.common import AnnouncementResponse, PaginatedResponse
from app.services.announcement_service import announcement_service
from app.utils.exceptions import ForbiddenError

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_my_announcements(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """내가 볼 수 있는 공지사항 목록을 조회합니다.

    List announcements visible to the current user (org-wide + my stores).

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)
        page: 페이지 번호 (Page number)
        per_page: 페이지당 항목 수 (Items per page)

    Returns:
        dict: 페이지네이션된 공지 목록 (Paginated announcement list)
    """
    announcements, total = await announcement_service.list_for_user(
        db,
        organization_id=current_user.organization_id,
        user_id=current_user.id,
        page=page,
        per_page=per_page,
    )

    items: list[dict] = []
    for a in announcements:
        response: dict = await announcement_service.build_response(db, a)
        items.append(response)

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/{announcement_id}", response_model=AnnouncementResponse)
async def get_my_announcement(
    announcement_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """공지사항 상세를 조회합니다 (매장 접근 권한 확인 포함).

    Get announcement detail with store access control.

    Args:
        announcement_id: 공지 UUID (Announcement UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)

    Returns:
        dict: 공지 상세 (Announcement detail)
    """
    announcement = await announcement_service.get_detail(
        db,
        announcement_id=announcement_id,
        organization_id=current_user.organization_id,
    )

    # 매장 공지의 경우 접근 권한 확인 — Check store access for store-specific announcements
    if announcement.store_id is not None:
        store_check = await db.execute(
            select(UserStore).where(
                UserStore.user_id == current_user.id,
                UserStore.store_id == announcement.store_id,
            )
        )
        if store_check.scalar_one_or_none() is None:
            raise ForbiddenError("이 공지사항에 대한 접근 권한이 없습니다 (No access to this announcement)")

    return await announcement_service.build_response(db, announcement)
