"""앱 Voice 라우터 — 직원용 Voice API.

App Voice Router — Employee-facing voice endpoints.
Any authenticated user can create and view their own voices.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.common import PaginatedResponse
from app.schemas.voice import VoiceCreate
from app.services.voice_service import voice_service

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_my_voices(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """내 Voice 목록 조회."""
    voices, total = await voice_service.list_for_user(
        db, current_user.organization_id, current_user.id, page, per_page
    )
    items = [await voice_service.build_response(db, v) for v in voices]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/{voice_id}")
async def get_my_voice(
    voice_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """내 Voice 상세 조회."""
    voice = await voice_service.get_detail(db, voice_id, current_user.organization_id)
    return await voice_service.build_response(db, voice)


@router.post("", status_code=201)
async def create_voice(
    data: VoiceCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """Voice 생성."""
    voice = await voice_service.create_voice(
        db, current_user.organization_id, data, current_user.id
    )
    await db.commit()
    return await voice_service.build_response(db, voice)
