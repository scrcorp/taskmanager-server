"""체크리스트 템플릿 연결 레포지토리.

Checklist Template Link Repository — DB queries for cl_template_links.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.checklist import ChecklistTemplateLink
from app.repositories.base import BaseRepository


class TemplateLinkRepository(BaseRepository[ChecklistTemplateLink]):

    def __init__(self) -> None:
        super().__init__(ChecklistTemplateLink)

    async def get_by_template(
        self, db: AsyncSession, template_id: UUID
    ) -> Sequence[ChecklistTemplateLink]:
        query: Select = (
            select(ChecklistTemplateLink)
            .where(ChecklistTemplateLink.template_id == template_id)
            .order_by(ChecklistTemplateLink.created_at.desc())
        )
        result = await db.execute(query)
        return result.scalars().all()

    async def get_by_store(
        self, db: AsyncSession, store_id: UUID
    ) -> Sequence[ChecklistTemplateLink]:
        query: Select = (
            select(ChecklistTemplateLink)
            .where(ChecklistTemplateLink.store_id == store_id)
            .order_by(ChecklistTemplateLink.created_at.desc())
        )
        result = await db.execute(query)
        return result.scalars().all()

    async def check_duplicate(
        self,
        db: AsyncSession,
        template_id: UUID,
        store_id: UUID,
        shift_id: UUID,
        position_id: UUID,
    ) -> bool:
        query: Select = (
            select(func.count())
            .select_from(ChecklistTemplateLink)
            .where(
                ChecklistTemplateLink.template_id == template_id,
                ChecklistTemplateLink.store_id == store_id,
                ChecklistTemplateLink.shift_id == shift_id,
                ChecklistTemplateLink.position_id == position_id,
            )
        )
        count: int = (await db.execute(query)).scalar() or 0
        return count > 0


# 싱글턴 인스턴스
template_link_repository: TemplateLinkRepository = TemplateLinkRepository()
