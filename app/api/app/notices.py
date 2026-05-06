"""앱 공지사항 라우터 — 사용자용 공지사항 조회 API.

App Notice Router — API endpoints for user's notice viewing.
Provides read-only access to org-wide and store-specific notices.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.communication import NoticeRead
from app.models.user import User
from app.models.user_store import UserStore
from app.schemas.common import NoticeResponse, MessageResponse, PaginatedResponse
from app.services.notice_service import notice_service
from app.utils.exceptions import ForbiddenError

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_my_notices(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """내가 볼 수 있는 공지사항 목록을 조회합니다.

    List notices visible to the current user (org-wide + my stores).

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)
        page: 페이지 번호 (Page number)
        per_page: 페이지당 항목 수 (Items per page)

    Returns:
        dict: 페이지네이션된 공지 목록 (Paginated notice list)
    """
    notices, total = await notice_service.list_for_user(
        db,
        organization_id=current_user.organization_id,
        user_id=current_user.id,
        page=page,
        per_page=per_page,
    )

    items: list[dict] = []
    for a in notices:
        response: dict = await notice_service.build_response(db, a)
        items.append(response)

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/{notice_id}", response_model=NoticeResponse)
async def get_my_notice(
    notice_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """공지사항 상세를 조회합니다 (매장 접근 권한 확인 포함).

    Get notice detail with store access control.

    Args:
        notice_id: 공지 UUID (Notice UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)

    Returns:
        dict: 공지 상세 (Notice detail)
    """
    notice = await notice_service.get_detail(
        db,
        notice_id=notice_id,
        organization_id=current_user.organization_id,
    )

    # 매장 공지의 경우 접근 권한 확인 — Check store access for store-specific notices
    if notice.store_id is not None:
        store_check = await db.execute(
            select(UserStore).where(
                UserStore.user_id == current_user.id,
                UserStore.store_id == notice.store_id,
            )
        )
        if store_check.scalar_one_or_none() is None:
            raise ForbiddenError("No access to this notice")

    return await notice_service.build_response(db, notice)


@router.post("/{notice_id}/read", response_model=MessageResponse)
async def mark_notice_read(
    notice_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """공지사항을 읽음 처리합니다.

    Mark an notice as read by the current user.
    Idempotent — re-reading does not create duplicate records.
    """
    existing = await db.execute(
        select(NoticeRead).where(
            NoticeRead.notice_id == notice_id,
            NoticeRead.user_id == current_user.id,
        )
    )
    if existing.scalar_one_or_none() is None:
        read_record = NoticeRead(
            notice_id=notice_id,
            user_id=current_user.id,
        )
        db.add(read_record)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    return {"message": "Marked as read"}
