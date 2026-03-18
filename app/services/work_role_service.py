"""업무 역할 서비스."""

from datetime import time
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import StoreWorkRole
from app.models.work import Position, Shift
from app.repositories.store_repository import store_repository
from app.repositories.work_role_repository import work_role_repository
from app.schemas.schedule import WorkRoleCreate, WorkRoleResponse, WorkRoleUpdate
from app.utils.exceptions import DuplicateError, NotFoundError


class WorkRoleService:

    @staticmethod
    def _parse_time(t: str | None) -> time | None:
        if t is None:
            return None
        parts = t.split(":")
        return time(int(parts[0]), int(parts[1]))

    @staticmethod
    def _format_time(t: time | None) -> str | None:
        if t is None:
            return None
        return t.strftime("%H:%M")

    async def _to_response(
        self, db: AsyncSession, wr: StoreWorkRole
    ) -> WorkRoleResponse:
        shift_result = await db.execute(
            select(Shift.name).where(Shift.id == wr.shift_id)
        )
        shift_name: str | None = shift_result.scalar()

        pos_result = await db.execute(
            select(Position.name).where(Position.id == wr.position_id)
        )
        position_name: str | None = pos_result.scalar()

        return WorkRoleResponse(
            id=str(wr.id),
            store_id=str(wr.store_id),
            shift_id=str(wr.shift_id),
            shift_name=shift_name,
            position_id=str(wr.position_id),
            position_name=position_name,
            name=wr.name,
            default_start_time=self._format_time(wr.default_start_time),
            default_end_time=self._format_time(wr.default_end_time),
            break_start_time=self._format_time(wr.break_start_time),
            break_end_time=self._format_time(wr.break_end_time),
            required_headcount=wr.required_headcount,
            default_checklist_id=str(wr.default_checklist_id)
            if wr.default_checklist_id
            else None,
            is_active=wr.is_active,
            sort_order=wr.sort_order,
            created_at=wr.created_at,
            updated_at=wr.updated_at,
        )

    async def _verify_store(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID
    ) -> None:
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if store is None:
            raise NotFoundError("Store not found")

    async def list_work_roles(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
    ) -> list[WorkRoleResponse]:
        await self._verify_store(db, store_id, organization_id)
        roles = await work_role_repository.get_by_store(db, store_id)
        return [await self._to_response(db, r) for r in roles]

    async def create_work_role(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
        data: WorkRoleCreate,
    ) -> WorkRoleResponse:
        await self._verify_store(db, store_id, organization_id)

        shift_id = UUID(data.shift_id)
        position_id = UUID(data.position_id)

        if await work_role_repository.check_duplicate(
            db, store_id, shift_id, position_id
        ):
            raise DuplicateError(
                "이 매장에 동일한 shift+position 조합이 이미 존재합니다"
            )

        try:
            wr = await work_role_repository.create(
                db,
                {
                    "store_id": store_id,
                    "shift_id": shift_id,
                    "position_id": position_id,
                    "name": data.name,
                    "default_start_time": self._parse_time(data.default_start_time),
                    "default_end_time": self._parse_time(data.default_end_time),
                    "break_start_time": self._parse_time(data.break_start_time),
                    "break_end_time": self._parse_time(data.break_end_time),
                    "required_headcount": data.required_headcount,
                    "default_checklist_id": UUID(data.default_checklist_id)
                    if data.default_checklist_id
                    else None,
                    "is_active": data.is_active,
                    "sort_order": data.sort_order,
                },
            )
            result = await self._to_response(db, wr)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def update_work_role(
        self,
        db: AsyncSession,
        work_role_id: UUID,
        organization_id: UUID,
        data: WorkRoleUpdate,
    ) -> WorkRoleResponse:
        wr = await work_role_repository.get_by_id(db, work_role_id)
        if wr is None:
            raise NotFoundError("Work role not found")
        await self._verify_store(db, wr.store_id, organization_id)

        update_data: dict = {}
        if data.name is not None:
            update_data["name"] = data.name
        if data.default_start_time is not None:
            update_data["default_start_time"] = self._parse_time(
                data.default_start_time
            )
        if data.default_end_time is not None:
            update_data["default_end_time"] = self._parse_time(data.default_end_time)
        if data.break_start_time is not None:
            update_data["break_start_time"] = self._parse_time(data.break_start_time)
        if data.break_end_time is not None:
            update_data["break_end_time"] = self._parse_time(data.break_end_time)
        if data.required_headcount is not None:
            update_data["required_headcount"] = data.required_headcount
        if data.default_checklist_id is not None:
            update_data["default_checklist_id"] = (
                UUID(data.default_checklist_id) if data.default_checklist_id else None
            )
        if data.is_active is not None:
            update_data["is_active"] = data.is_active
        if data.sort_order is not None:
            update_data["sort_order"] = data.sort_order

        if not update_data:
            return await self._to_response(db, wr)

        try:
            updated = await work_role_repository.update(db, work_role_id, update_data)
            if updated is None:
                raise NotFoundError("Work role not found")
            result = await self._to_response(db, updated)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def reorder_work_roles(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
        items: list[dict],
    ) -> list[WorkRoleResponse]:
        """Bulk update sort_order for work roles."""
        await self._verify_store(db, store_id, organization_id)
        try:
            for item in items:
                wr_id = UUID(item["id"])
                wr = await work_role_repository.get_by_id(db, wr_id)
                if wr is None or wr.store_id != store_id:
                    continue
                await work_role_repository.update(db, wr_id, {"sort_order": item["sort_order"]})
            roles = await work_role_repository.get_by_store(db, store_id)
            result = [await self._to_response(db, r) for r in roles]
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def delete_work_role(
        self,
        db: AsyncSession,
        work_role_id: UUID,
        organization_id: UUID,
    ) -> None:
        wr = await work_role_repository.get_by_id(db, work_role_id)
        if wr is None:
            raise NotFoundError("Work role not found")
        await self._verify_store(db, wr.store_id, organization_id)
        try:
            await work_role_repository.delete(db, work_role_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise


work_role_service: WorkRoleService = WorkRoleService()
