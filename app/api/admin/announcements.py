"""관리자 공지사항 라우터 — 공지사항 관리 API.

Admin Announcement Router — API endpoints for announcement management.
Provides CRUD operations for organization-wide and brand-specific announcements.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_supervisor
from app.database import get_db
from app.models.user import User
from app.schemas.common import (
    AnnouncementCreate,
    AnnouncementResponse,
    AnnouncementUpdate,
    MessageResponse,
    PaginatedResponse,
)
from app.services.announcement_service import announcement_service

router: APIRouter = APIRouter()


@router.get("/", response_model=PaginatedResponse)
async def list_announcements(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """공지사항 목록을 조회합니다.

    List announcements for the organization.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)
        page: 페이지 번호 (Page number)
        per_page: 페이지당 항목 수 (Items per page)

    Returns:
        dict: 페이지네이션된 공지 목록 (Paginated announcement list)
    """
    announcements, total = await announcement_service.list_announcements(
        db,
        organization_id=current_user.organization_id,
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
async def get_announcement(
    announcement_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """공지사항 상세를 조회합니다.

    Get announcement detail.

    Args:
        announcement_id: 공지 UUID 문자열 (Announcement UUID string)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 공지 상세 (Announcement detail)
    """
    announcement = await announcement_service.get_detail(
        db,
        announcement_id=announcement_id,
        organization_id=current_user.organization_id,
    )
    return await announcement_service.build_response(db, announcement)


@router.post("/", response_model=AnnouncementResponse, status_code=201)
async def create_announcement(
    data: AnnouncementCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """새 공지사항을 생성합니다.

    Create a new announcement.

    Args:
        data: 공지 생성 데이터 (Announcement creation data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 생성된 공지 (Created announcement)
    """
    announcement = await announcement_service.create_announcement(
        db,
        organization_id=current_user.organization_id,
        data=data,
        created_by=current_user.id,
    )
    await db.commit()

    return await announcement_service.build_response(db, announcement)


@router.put("/{announcement_id}", response_model=AnnouncementResponse)
async def update_announcement(
    announcement_id: UUID,
    data: AnnouncementUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """공지사항을 업데이트합니다.

    Update an announcement.

    Args:
        announcement_id: 공지 UUID 문자열 (Announcement UUID string)
        data: 업데이트 데이터 (Update data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 업데이트된 공지 (Updated announcement)
    """
    announcement = await announcement_service.update_announcement(
        db,
        announcement_id=announcement_id,
        organization_id=current_user.organization_id,
        data=data,
    )
    await db.commit()

    return await announcement_service.build_response(db, announcement)


@router.delete("/{announcement_id}", response_model=MessageResponse)
async def delete_announcement(
    announcement_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """공지사항을 삭제합니다.

    Delete an announcement.

    Args:
        announcement_id: 공지 UUID 문자열 (Announcement UUID string)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 삭제 결과 메시지 (Deletion result message)
    """
    await announcement_service.delete_announcement(
        db,
        announcement_id=announcement_id,
        organization_id=current_user.organization_id,
    )
    await db.commit()

    return {"message": "공지사항이 삭제되었습니다 (Announcement deleted)"}
