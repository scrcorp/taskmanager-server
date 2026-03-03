"""Voice 서비스.

Voice service — Business logic for voice CRUD.
"""

from datetime import datetime, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import Voice
from app.models.user import User
from app.repositories.voice_repository import voice_repository
from app.schemas.voice import VoiceCreate, VoiceUpdate
from app.utils.exceptions import NotFoundError


class VoiceService:

    async def build_response(self, db: AsyncSession, voice: Voice) -> dict:
        creator_result = await db.execute(
            select(User.full_name).where(User.id == voice.created_by)
        )
        created_by_name: str = creator_result.scalar() or "Unknown"

        resolved_by_name: str | None = None
        if voice.resolved_by:
            r = await db.execute(
                select(User.full_name).where(User.id == voice.resolved_by)
            )
            resolved_by_name = r.scalar()

        return {
            "id": str(voice.id),
            "title": voice.title,
            "description": voice.description,
            "category": voice.category,
            "status": voice.status,
            "priority": voice.priority,
            "store_id": str(voice.store_id) if voice.store_id else None,
            "created_by": str(voice.created_by),
            "created_by_name": created_by_name,
            "resolved_by": str(voice.resolved_by) if voice.resolved_by else None,
            "resolved_by_name": resolved_by_name,
            "resolved_at": voice.resolved_at,
            "created_at": voice.created_at,
            "updated_at": voice.updated_at,
        }

    # --- Admin ---

    async def list_voices(
        self,
        db: AsyncSession,
        organization_id: UUID,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Voice], int]:
        return await voice_repository.get_by_org(
            db, organization_id, status, page, per_page
        )

    async def get_detail(
        self,
        db: AsyncSession,
        voice_id: UUID,
        organization_id: UUID,
    ) -> Voice:
        voice = await voice_repository.get_by_id(db, voice_id, organization_id)
        if voice is None:
            raise NotFoundError("Voice를 찾을 수 없습니다 (Voice not found)")
        return voice

    async def create_voice(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: VoiceCreate,
        created_by: UUID,
    ) -> Voice:
        store_id = UUID(data.store_id) if data.store_id else None
        return await voice_repository.create(
            db,
            {
                "organization_id": organization_id,
                "store_id": store_id,
                "title": data.title,
                "description": data.description,
                "category": data.category,
                "priority": data.priority,
                "created_by": created_by,
            },
        )

    async def update_voice(
        self,
        db: AsyncSession,
        voice_id: UUID,
        organization_id: UUID,
        data: VoiceUpdate,
        current_user_id: UUID,
    ) -> Voice:
        update_data = data.model_dump(exclude_unset=True)

        # Auto-set resolved_by/resolved_at when status changes to resolved
        if update_data.get("status") == "resolved":
            update_data["resolved_by"] = current_user_id
            update_data["resolved_at"] = datetime.now(timezone.utc)

        if "store_id" in update_data:
            val = update_data["store_id"]
            update_data["store_id"] = UUID(val) if val else None

        updated = await voice_repository.update(
            db, voice_id, update_data, organization_id
        )
        if updated is None:
            raise NotFoundError("Voice를 찾을 수 없습니다 (Voice not found)")
        return updated

    async def delete_voice(
        self,
        db: AsyncSession,
        voice_id: UUID,
        organization_id: UUID,
    ) -> bool:
        deleted = await voice_repository.delete(db, voice_id, organization_id)
        if not deleted:
            raise NotFoundError("Voice를 찾을 수 없습니다 (Voice not found)")
        return deleted

    # --- App (사용자용) ---

    async def list_for_user(
        self,
        db: AsyncSession,
        organization_id: UUID,
        user_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Voice], int]:
        return await voice_repository.get_by_user(
            db, organization_id, user_id, page, per_page
        )


voice_service: VoiceService = VoiceService()
