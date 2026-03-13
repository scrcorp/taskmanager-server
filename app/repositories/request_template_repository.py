"""스케줄 신청 템플릿 레포지토리."""

from uuid import UUID
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import ScheduleRequestTemplate, ScheduleRequestTemplateItem
from app.repositories.base import BaseRepository


class RequestTemplateRepository(BaseRepository[ScheduleRequestTemplate]):

    def __init__(self) -> None:
        super().__init__(ScheduleRequestTemplate)

    async def get_by_user(
        self, db: AsyncSession, user_id: UUID,
    ) -> list[ScheduleRequestTemplate]:
        result = await db.execute(
            select(ScheduleRequestTemplate)
            .where(ScheduleRequestTemplate.user_id == user_id)
            .order_by(ScheduleRequestTemplate.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_user_store(
        self, db: AsyncSession, user_id: UUID, store_id: UUID,
    ) -> list[ScheduleRequestTemplate]:
        result = await db.execute(
            select(ScheduleRequestTemplate)
            .where(ScheduleRequestTemplate.user_id == user_id, ScheduleRequestTemplate.store_id == store_id)
            .order_by(ScheduleRequestTemplate.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_items(
        self, db: AsyncSession, template_id: UUID,
    ) -> list[ScheduleRequestTemplateItem]:
        result = await db.execute(
            select(ScheduleRequestTemplateItem)
            .where(ScheduleRequestTemplateItem.template_id == template_id)
            .order_by(ScheduleRequestTemplateItem.day_of_week)
        )
        return list(result.scalars().all())

    async def create_item(
        self, db: AsyncSession, data: dict,
    ) -> ScheduleRequestTemplateItem:
        item = ScheduleRequestTemplateItem(**data)
        db.add(item)
        await db.flush()
        await db.refresh(item)
        return item

    async def delete_items(
        self, db: AsyncSession, template_id: UUID,
    ) -> None:
        await db.execute(
            delete(ScheduleRequestTemplateItem)
            .where(ScheduleRequestTemplateItem.template_id == template_id)
        )


request_template_repository: RequestTemplateRepository = RequestTemplateRepository()
