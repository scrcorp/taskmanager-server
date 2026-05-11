"""관리자 Voice 라우터 — Voice 관리 API.

Admin Voice Router — CRUD endpoints for voice management.
All admin roles (SV+) can view. GM+ can update status. Any authenticated user can create.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.common import MessageResponse, PaginatedResponse
from app.schemas.voice import VoiceCreate, VoiceUpdate
from app.services.voice_service import voice_service

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_voices(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:read"))],
    status: str | None = Query(None),
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """Voice 목록 조회. SV+ 가능."""
    voices, total = await voice_service.list_voices(
        db, current_user.organization_id, status, page, per_page
    )
    items = await voice_service.build_responses_batch(db, voices)
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/{voice_id}")
async def get_voice(
    voice_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:read"))],
) -> dict:
    """Voice 상세 조회."""
    voice = await voice_service.get_detail(db, voice_id, current_user.organization_id)
    return await voice_service.build_response(db, voice)


@router.post("", status_code=201)
async def create_voice(
    data: VoiceCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """Voice 생성. 전 역할 가능."""
    voice = await voice_service.create_voice(
        db, current_user.organization_id, data, current_user.id
    )
    return await voice_service.build_response(db, voice)


@router.put("/{voice_id}")
async def update_voice(
    voice_id: UUID,
    data: VoiceUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:update"))],
) -> dict:
    """Voice 수정. GM+ 가능."""
    voice = await voice_service.update_voice(
        db, voice_id, current_user.organization_id, data, current_user.id
    )
    return await voice_service.build_response(db, voice)


@router.delete("/{voice_id}", response_model=MessageResponse)
async def delete_voice(
    voice_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:delete"))],
) -> dict:
    """Voice 삭제. GM+ 가능."""
    await voice_service.delete_voice(db, voice_id, current_user.organization_id)
    return {"message": "Voice deleted"}
