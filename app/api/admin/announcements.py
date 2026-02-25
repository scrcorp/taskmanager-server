"""관리자 공지사항 라우터 — 공지사항 관리 API.

Admin Announcement Router — API endpoints for announcement management.
Provides CRUD operations for organization-wide and store-specific announcements.

Permission Matrix (역할별 권한 설계):
    - 공지사항 작성/수정/삭제: Owner + GM
    - 공지사항 조회: Owner + GM + SV (전 관리 역할)
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.communication import AnnouncementRead
from app.models.user import User
from app.schemas.common import (
    AnnouncementCreate,
    AnnouncementResponse,
    AnnouncementUpdate,
    MessageResponse,
    PaginatedResponse,
)
from app.schemas.announcement_read import AnnouncementReadResponse
from app.services.announcement_service import announcement_service

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_announcements(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("announcements:read"))],
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """공지사항 목록을 조회합니다. 전 관리 역할 조회 가능.

    List announcements for the organization. All admin roles can read.
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
    current_user: Annotated[User, Depends(require_permission("announcements:read"))],
) -> dict:
    """공지사항 상세를 조회합니다.

    Get announcement detail.
    """
    announcement = await announcement_service.get_detail(
        db,
        announcement_id=announcement_id,
        organization_id=current_user.organization_id,
    )
    return await announcement_service.build_response(db, announcement)


@router.post("", response_model=AnnouncementResponse, status_code=201)
async def create_announcement(
    data: AnnouncementCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("announcements:create"))],
) -> dict:
    """새 공지사항을 생성합니다. Owner + GM만 가능.

    Create a new announcement. Owner + GM only.
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
    current_user: Annotated[User, Depends(require_permission("announcements:update"))],
) -> dict:
    """공지사항을 업데이트합니다. Owner + GM만 가능.

    Update an announcement. Owner + GM only.
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
    current_user: Annotated[User, Depends(require_permission("announcements:delete"))],
) -> dict:
    """공지사항을 삭제합니다. Owner + GM만 가능.

    Delete an announcement. Owner + GM only.
    """
    await announcement_service.delete_announcement(
        db,
        announcement_id=announcement_id,
        organization_id=current_user.organization_id,
    )
    await db.commit()

    return {"message": "공지사항이 삭제되었습니다 (Announcement deleted)"}


@router.get("/{announcement_id}/reads", response_model=list[AnnouncementReadResponse])
async def get_announcement_reads(
    announcement_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("announcements:read"))],
) -> list[AnnouncementReadResponse]:
    """공지사항 읽음 현황을 조회합니다. Owner + GM만 가능."""
    query = (
        select(AnnouncementRead)
        .where(AnnouncementRead.announcement_id == announcement_id)
        .order_by(AnnouncementRead.read_at)
    )
    result = await db.execute(query)
    reads = list(result.scalars().all())

    responses: list[AnnouncementReadResponse] = []
    for r in reads:
        user_result = await db.execute(select(User).where(User.id == r.user_id))
        user = user_result.scalar_one_or_none()
        responses.append(AnnouncementReadResponse(
            user_id=str(r.user_id),
            user_name=user.full_name if user else None,
            read_at=r.read_at,
        ))
    return responses
