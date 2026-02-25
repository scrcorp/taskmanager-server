"""체크리스트 템플릿 연결 서비스.

Template Link Service — Business logic for checklist template link CRUD.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.checklist import ChecklistTemplate, ChecklistTemplateLink
from app.models.organization import Store
from app.models.work import Position, Shift
from app.repositories.template_link_repository import template_link_repository
from app.schemas.common import TemplateLinkCreate
from app.utils.exceptions import DuplicateError, ForbiddenError, NotFoundError

# 요일 약어 매핑
VALID_DAYS: set[str] = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


class TemplateLinkService:

    async def _validate_store_ownership(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID
    ) -> Store:
        result = await db.execute(select(Store).where(Store.id == store_id))
        store: Store | None = result.scalar_one_or_none()
        if store is None:
            raise NotFoundError("Store not found")
        if store.organization_id != organization_id:
            raise ForbiddenError("No permission for this store")
        return store

    async def create_link(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: TemplateLinkCreate,
    ) -> ChecklistTemplateLink:
        template_id = UUID(data.template_id)
        store_id = UUID(data.store_id)
        shift_id = UUID(data.shift_id)
        position_id = UUID(data.position_id)

        # Validate store ownership
        await self._validate_store_ownership(db, store_id, organization_id)

        # Validate template exists
        tmpl_result = await db.execute(
            select(ChecklistTemplate).where(ChecklistTemplate.id == template_id)
        )
        if tmpl_result.scalar_one_or_none() is None:
            raise NotFoundError("Checklist template not found")

        # Check duplicate
        is_dup = await template_link_repository.check_duplicate(
            db, template_id, store_id, shift_id, position_id
        )
        if is_dup:
            raise DuplicateError("This template link combination already exists")

        # Normalize repeat_days
        repeat_type = data.repeat_type or "daily"
        repeat_days = None
        if repeat_type == "custom" and data.repeat_days:
            repeat_days = [d.lower() for d in data.repeat_days if d.lower() in VALID_DAYS]
            if not repeat_days:
                repeat_type = "daily"
                repeat_days = None

        link = await template_link_repository.create(db, {
            "template_id": template_id,
            "store_id": store_id,
            "shift_id": shift_id,
            "position_id": position_id,
            "repeat_type": repeat_type,
            "repeat_days": repeat_days,
        })
        return link

    async def list_links(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        template_id: UUID | None = None,
    ) -> Sequence[ChecklistTemplateLink]:
        if store_id:
            await self._validate_store_ownership(db, store_id, organization_id)
            return await template_link_repository.get_by_store(db, store_id)
        if template_id:
            return await template_link_repository.get_by_template(db, template_id)
        # Fallback: get all for org via stores
        store_result = await db.execute(
            select(Store.id).where(Store.organization_id == organization_id)
        )
        store_ids = [r for r in store_result.scalars().all()]
        all_links: list[ChecklistTemplateLink] = []
        for sid in store_ids:
            links = await template_link_repository.get_by_store(db, sid)
            all_links.extend(links)
        return all_links

    async def delete_link(
        self,
        db: AsyncSession,
        link_id: UUID,
        organization_id: UUID,
    ) -> bool:
        link: ChecklistTemplateLink | None = await template_link_repository.get_by_id(db, link_id)
        if link is None:
            raise NotFoundError("Template link not found")
        await self._validate_store_ownership(db, link.store_id, organization_id)
        return await template_link_repository.delete(db, link_id)

    async def build_response(
        self, db: AsyncSession, link: ChecklistTemplateLink
    ) -> dict:
        # Template title
        tmpl_result = await db.execute(
            select(ChecklistTemplate.title).where(ChecklistTemplate.id == link.template_id)
        )
        template_title = tmpl_result.scalar() or ""

        # Store name
        store_result = await db.execute(select(Store.name).where(Store.id == link.store_id))
        store_name = store_result.scalar() or ""

        # Shift name
        shift_result = await db.execute(select(Shift.name).where(Shift.id == link.shift_id))
        shift_name = shift_result.scalar() or ""

        # Position name
        pos_result = await db.execute(select(Position.name).where(Position.id == link.position_id))
        position_name = pos_result.scalar() or ""

        return {
            "id": str(link.id),
            "template_id": str(link.template_id),
            "template_title": template_title,
            "store_id": str(link.store_id),
            "store_name": store_name,
            "shift_id": str(link.shift_id),
            "shift_name": shift_name,
            "position_id": str(link.position_id),
            "position_name": position_name,
            "repeat_type": link.repeat_type,
            "repeat_days": link.repeat_days,
            "is_active": link.is_active,
            "created_at": link.created_at,
        }


# 싱글턴 인스턴스
template_link_service: TemplateLinkService = TemplateLinkService()
