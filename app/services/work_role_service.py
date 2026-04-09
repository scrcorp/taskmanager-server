"""업무 역할 서비스."""

from datetime import date, datetime, time, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.checklist import ChecklistInstance
from app.models.schedule import Schedule, StoreWorkRole
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
            headcount=wr.headcount,
            use_per_day_headcount=wr.use_per_day_headcount,
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
                    "headcount": data.headcount or {"all": 1, "sun": 1, "mon": 1, "tue": 1, "wed": 1, "thu": 1, "fri": 1, "sat": 1},
                    "use_per_day_headcount": data.use_per_day_headcount,
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

        # default_checklist_id가 null → 값으로 변경되는지 추적
        # Track whether default_checklist_id is transitioning from null to a value
        prev_checklist_id: UUID | None = wr.default_checklist_id

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
        if data.headcount is not None:
            update_data["headcount"] = data.headcount
        if data.use_per_day_headcount is not None:
            update_data["use_per_day_headcount"] = data.use_per_day_headcount
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

            # null → 값으로 변경된 경우 미래 스케줄에 cl_instance 자동 생성
            # Auto-create cl_instances for future schedules when checklist is newly linked
            new_checklist_id: UUID | None = updated.default_checklist_id
            if prev_checklist_id is None and new_checklist_id is not None:
                await self._create_instances_for_future_schedules(
                    db, work_role_id, new_checklist_id
                )

            result = await self._to_response(db, updated)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    @staticmethod
    async def _create_instances_for_future_schedules(
        db: AsyncSession,
        work_role_id: UUID,
        checklist_id: UUID,
    ) -> None:
        """work_role에 default_checklist_id가 새로 연결될 때 미래 스케줄에 cl_instance를 생성합니다.

        Conditions:
        - Schedule has this work_role_id
        - work_date >= today
        - No existing cl_instance for this schedule
        - Schedule status is not 'cancelled' or 'deleted'

        Auto-creates cl_instance + cl_instance_items via checklist_instance_service.
        """
        from app.services.checklist_instance_service import checklist_instance_service

        today: date = datetime.now(timezone.utc).date()

        # 미래 스케줄 중 cl_instance가 없는 것 조회
        # Find future schedules for this work_role with no existing cl_instance
        result = await db.execute(
            select(Schedule).where(
                Schedule.work_role_id == work_role_id,
                Schedule.work_date >= today,
                Schedule.status.notin_(["cancelled", "deleted"]),
                ~Schedule.id.in_(
                    select(ChecklistInstance.schedule_id).where(
                        ChecklistInstance.schedule_id.isnot(None)
                    )
                ),
            )
        )
        schedules = result.scalars().all()

        for sched in schedules:
            # store_id / user_id가 None이면 체크리스트 인스턴스 생성 불가 — skip
            if sched.store_id is None or sched.user_id is None:
                continue
            await checklist_instance_service.create_for_schedule(
                db,
                schedule_id=sched.id,
                organization_id=sched.organization_id,
                store_id=sched.store_id,
                user_id=sched.user_id,
                work_date=sched.work_date,
                work_role_id=work_role_id,
            )

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
