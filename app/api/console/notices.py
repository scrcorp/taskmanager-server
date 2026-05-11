"""관리자 공지사항 라우터 — 공지사항 관리 API.

Admin Notice Router — API endpoints for notice management.
Provides CRUD operations for organization-wide and store-specific notices.

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
from app.models.communication import NoticeRead
from app.models.user import User
from app.schemas.common import (
    NoticeCreate,
    NoticeResponse,
    NoticeUpdate,
    MessageResponse,
    PaginatedResponse,
)
from app.schemas.notice_read import NoticeReadResponse
from app.services.notice_service import notice_service

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_notices(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("notices:read"))],
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """공지사항 목록을 조회합니다. 전 관리 역할 조회 가능.

    List notices for the organization. All admin roles can read.
    """
    notices, total = await notice_service.list_notices(
        db,
        organization_id=current_user.organization_id,
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
async def get_notice(
    notice_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("notices:read"))],
) -> dict:
    """공지사항 상세를 조회합니다.

    Get notice detail.
    """
    notice = await notice_service.get_detail(
        db,
        notice_id=notice_id,
        organization_id=current_user.organization_id,
    )
    return await notice_service.build_response(db, notice)


@router.post("", response_model=NoticeResponse, status_code=201)
async def create_notice(
    data: NoticeCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("notices:create"))],
) -> dict:
    """새 공지사항을 생성합니다. Owner + GM만 가능.

    Create a new notice. Owner + GM only.
    """
    notice = await notice_service.create_notice(
        db,
        organization_id=current_user.organization_id,
        data=data,
        created_by=current_user.id,
    )

    return await notice_service.build_response(db, notice)


@router.put("/{notice_id}", response_model=NoticeResponse)
async def update_notice(
    notice_id: UUID,
    data: NoticeUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("notices:update"))],
) -> dict:
    """공지사항을 업데이트합니다. Owner + GM만 가능.

    Update an notice. Owner + GM only.
    """
    notice = await notice_service.update_notice(
        db,
        notice_id=notice_id,
        organization_id=current_user.organization_id,
        data=data,
    )

    return await notice_service.build_response(db, notice)


@router.delete("/{notice_id}", response_model=MessageResponse)
async def delete_notice(
    notice_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("notices:delete"))],
) -> dict:
    """공지사항을 삭제합니다. Owner + GM만 가능.

    Delete an notice. Owner + GM only.
    """
    await notice_service.delete_notice(
        db,
        notice_id=notice_id,
        organization_id=current_user.organization_id,
    )

    return {"message": "Notice deleted"}


@router.get("/{notice_id}/reads", response_model=list[NoticeReadResponse])
async def get_notice_reads(
    notice_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("notices:read"))],
) -> list[NoticeReadResponse]:
    """공지사항 읽음 현황을 조회합니다. Owner + GM만 가능."""
    query = (
        select(NoticeRead)
        .where(NoticeRead.notice_id == notice_id)
        .order_by(NoticeRead.read_at)
    )
    result = await db.execute(query)
    reads = list(result.scalars().all())

    responses: list[NoticeReadResponse] = []
    for r in reads:
        user_result = await db.execute(select(User).where(User.id == r.user_id))
        user = user_result.scalar_one_or_none()
        responses.append(NoticeReadResponse(
            user_id=str(r.user_id),
            user_name=user.full_name if user else None,
            read_at=r.read_at,
        ))
    return responses
