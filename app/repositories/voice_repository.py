"""Voice 레포지토리.

Voice repository — Handles voices DB queries.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import Voice
from app.repositories.base import BaseRepository


class VoiceRepository(BaseRepository[Voice]):

    def __init__(self) -> None:
        super().__init__(Voice)

    async def get_by_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Voice], int]:
        query: Select = (
            select(Voice)
            .where(Voice.organization_id == organization_id)
            .order_by(Voice.created_at.desc())
        )
        if status:
            query = query.where(Voice.status == status)
        return await self.get_paginated(db, query, page, per_page)

    async def get_by_user(
        self,
        db: AsyncSession,
        organization_id: UUID,
        user_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Voice], int]:
        query: Select = (
            select(Voice)
            .where(
                Voice.organization_id == organization_id,
                Voice.created_by == user_id,
            )
            .order_by(Voice.created_at.desc())
        )
        return await self.get_paginated(db, query, page, per_page)


voice_repository: VoiceRepository = VoiceRepository()
