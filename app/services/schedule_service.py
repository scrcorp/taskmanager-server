"""스케줄 서비스 — 확정 스케줄 비즈니스 로직."""

from datetime import date, time
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Store
from app.models.schedule import Schedule, StoreWorkRole
from app.models.user import User
from app.models.work import Shift, Position
from app.repositories.break_rule_repository import break_rule_repository
from app.repositories.schedule_repository import schedule_repository
from app.repositories.work_role_repository import work_role_repository
from app.schemas.schedule import (
    ScheduleCreate, ScheduleResponse, ScheduleUpdate,
    ScheduleValidation, FinalizeResult,
)
from app.utils.exceptions import BadRequestError, NotFoundError


MAX_DAILY_MINUTES = 720  # 12h default
MAX_WEEKLY_MINUTES = 2400  # 40h default


class ScheduleService:

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

    @staticmethod
    def _time_to_minutes(t: time) -> int:
        return t.hour * 60 + t.minute

    @staticmethod
    def _calc_net_minutes(start: time, end: time, break_start: time | None, break_end: time | None) -> int:
        start_m = start.hour * 60 + start.minute
        end_m = end.hour * 60 + end.minute
        if end_m <= start_m:
            end_m += 24 * 60  # overnight
        total = end_m - start_m
        if break_start and break_end:
            bs = break_start.hour * 60 + break_start.minute
            be = break_end.hour * 60 + break_end.minute
            if be <= bs:
                be += 24 * 60
            total -= (be - bs)
        return max(total, 0)

    async def _to_response(self, db: AsyncSession, entry: Schedule) -> ScheduleResponse:
        # User name
        user_result = await db.execute(select(User.full_name).where(User.id == entry.user_id))
        user_name: str | None = user_result.scalar()
        # Store name
        store_result = await db.execute(select(Store.name).where(Store.id == entry.store_id))
        store_name: str | None = store_result.scalar()
        # Work role name
        work_role_name: str | None = None
        if entry.work_role_id:
            wr_result = await db.execute(select(StoreWorkRole).where(StoreWorkRole.id == entry.work_role_id))
            wr_obj = wr_result.scalar_one_or_none()
            if wr_obj:
                if wr_obj.name:
                    work_role_name = wr_obj.name
                else:
                    s = await db.execute(select(Shift.name).where(Shift.id == wr_obj.shift_id))
                    p = await db.execute(select(Position.name).where(Position.id == wr_obj.position_id))
                    sn = s.scalar() or ""
                    pn = p.scalar() or ""
                    work_role_name = f"{sn} - {pn}"

        return ScheduleResponse(
            id=str(entry.id),
            organization_id=str(entry.organization_id),
            request_id=str(entry.request_id) if entry.request_id else None,
            user_id=str(entry.user_id),
            user_name=user_name,
            store_id=str(entry.store_id),
            store_name=store_name,
            work_role_id=str(entry.work_role_id) if entry.work_role_id else None,
            work_role_name=work_role_name,
            work_date=entry.work_date,
            start_time=self._format_time(entry.start_time),  # type: ignore[arg-type]
            end_time=self._format_time(entry.end_time),  # type: ignore[arg-type]
            break_start_time=self._format_time(entry.break_start_time),
            break_end_time=self._format_time(entry.break_end_time),
            net_work_minutes=entry.net_work_minutes,
            status=entry.status,
            created_by=str(entry.created_by) if entry.created_by else None,
            approved_by=str(entry.approved_by) if entry.approved_by else None,
            note=entry.note,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
        )

    async def _validate_entry(
        self,
        db: AsyncSession,
        user_id: UUID,
        store_id: UUID,
        work_date: date,
        start_time: time,
        end_time: time,
        break_start: time | None,
        break_end: time | None,
        force: bool = False,
        exclude_id: UUID | None = None,
    ) -> ScheduleValidation:
        errors: list[str] = []
        warnings: list[str] = []

        start_m = self._time_to_minutes(start_time)
        end_m = self._time_to_minutes(end_time)

        # 1. Time overlap check
        if await schedule_repository.check_time_overlap(
            db, user_id, work_date, start_m, end_m, exclude_id
        ):
            errors.append("해당 직원의 같은 날짜에 시간이 겹치는 스케줄이 있습니다")

        net = self._calc_net_minutes(start_time, end_time, break_start, break_end)

        # 2. Daily total check
        break_rule = await break_rule_repository.get_by_store(db, store_id)
        if not force:
            max_daily = break_rule.max_daily_work_minutes if break_rule else MAX_DAILY_MINUTES
            existing_daily = await schedule_repository.get_daily_minutes(db, user_id, work_date, exclude_id)
            if existing_daily + net > max_daily:
                warnings.append(f"Daily work hours exceeded: {(existing_daily + net) // 60}h > {max_daily // 60}h")

        # 3. Weekly total check
        if not force:
            existing_weekly = await schedule_repository.get_weekly_minutes(db, user_id, work_date, exclude_id)
            if existing_weekly + net > MAX_WEEKLY_MINUTES:
                warnings.append(f"Weekly work hours exceeded: {(existing_weekly + net) // 60}h > {MAX_WEEKLY_MINUTES // 60}h")

        # 4. Break suggestion
        if not force and break_rule:
            if net > break_rule.max_continuous_minutes and break_start is None:
                warnings.append(f"Continuous work exceeds {break_rule.max_continuous_minutes}min — a break is recommended")

        valid = len(errors) == 0 and (force or len(warnings) == 0)
        return ScheduleValidation(valid=valid, warnings=warnings, errors=errors)

    async def list_entries(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        user_id: UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 100,
        sort_desc: bool = False,
    ) -> tuple[list[ScheduleResponse], int]:
        entries, total = await schedule_repository.get_by_filters(
            db, organization_id, store_id, user_id,
            date_from, date_to, status, page, per_page,
            sort_desc=sort_desc,
        )
        responses = [await self._to_response(db, e) for e in entries]
        return responses, total

    async def create_entry(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: ScheduleCreate,
        created_by: UUID,
    ) -> ScheduleResponse:
        store_id = UUID(data.store_id)
        user_id = UUID(data.user_id)
        start_time = self._parse_time(data.start_time)  # type: ignore[arg-type]
        end_time = self._parse_time(data.end_time)  # type: ignore[arg-type]
        break_start = self._parse_time(data.break_start_time)
        break_end = self._parse_time(data.break_end_time)

        if start_time is None or end_time is None:
            raise BadRequestError("start_time and end_time are required")

        # Validate
        validation = await self._validate_entry(
            db, user_id, store_id, data.work_date,
            start_time, end_time, break_start, break_end, data.force,
        )
        if not validation.valid:
            detail = "; ".join(validation.errors + validation.warnings)
            raise BadRequestError(f"Validation failed: {detail}")

        net = self._calc_net_minutes(start_time, end_time, break_start, break_end)

        try:
            entry = await schedule_repository.create(db, {
                "organization_id": organization_id,
                "request_id": UUID(data.request_id) if data.request_id else None,
                "user_id": user_id,
                "store_id": store_id,
                "work_role_id": UUID(data.work_role_id) if data.work_role_id else None,
                "work_date": data.work_date,
                "start_time": start_time,
                "end_time": end_time,
                "break_start_time": break_start,
                "break_end_time": break_end,
                "net_work_minutes": net,
                "status": "confirmed",
                "created_by": created_by,
            })

            # 체크리스트 인스턴스 자동 생성
            from app.services.checklist_instance_service import checklist_instance_service
            await checklist_instance_service.create_for_schedule(
                db,
                schedule_id=entry.id,
                organization_id=organization_id,
                store_id=store_id,
                user_id=user_id,
                work_date=data.work_date,
                work_role_id=UUID(data.work_role_id) if data.work_role_id else None,
            )

            result = await self._to_response(db, entry)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def bulk_create(
        self,
        db: AsyncSession,
        organization_id: UUID,
        entries_data: list[ScheduleCreate],
        created_by: UUID,
        skip_on_conflict: bool = False,
    ) -> dict:
        """벌크 스케줄 생성. skip_on_conflict=True면 겹치는 건은 건너뛰고 나머지 생성."""
        from app.schemas.schedule import ScheduleBulkResult

        created = 0
        skipped = 0
        failed = 0
        errors: list[str] = []
        items: list = []

        for i, data in enumerate(entries_data):
            try:
                result = await self.create_entry(db, organization_id, data, created_by)
                items.append(result)
                created += 1
            except BadRequestError as e:
                if skip_on_conflict:
                    skipped += 1
                    errors.append(f"[{i}] skipped: {e.detail}")
                else:
                    failed += 1
                    errors.append(f"[{i}] failed: {e.detail}")

        return ScheduleBulkResult(
            created=created, skipped=skipped, failed=failed,
            errors=errors, items=items,
        )

    async def generate_from_requests(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID,
        date_from: date,
        date_to: date,
        created_by: UUID,
    ) -> list[ScheduleResponse]:
        """신청(accepted) 기반으로 스케줄 자동 생성."""
        from app.repositories.schedule_request_repository import schedule_request_repository as sr_repo
        requests, _ = await sr_repo.get_by_filters(
            db, store_id=store_id, date_from=date_from, date_to=date_to, per_page=500
        )

        results = []
        try:
            for req in requests:
                if req.status not in ("submitted", "accepted"):
                    continue
                # Get work role defaults
                start_time = req.preferred_start_time
                end_time = req.preferred_end_time
                break_start = None
                break_end = None

                if req.work_role_id:
                    wr = await work_role_repository.get_by_id(db, req.work_role_id)
                    if wr:
                        if start_time is None:
                            start_time = wr.default_start_time
                        if end_time is None:
                            end_time = wr.default_end_time
                        break_start = wr.break_start_time
                        break_end = wr.break_end_time

                if start_time is None or end_time is None:
                    continue  # Skip if no time info

                net = self._calc_net_minutes(start_time, end_time, break_start, break_end)
                entry = await schedule_repository.create(db, {
                    "organization_id": organization_id,
                    "request_id": req.id,
                    "user_id": req.user_id,
                    "store_id": req.store_id,
                    "work_role_id": req.work_role_id,
                    "work_date": req.work_date,
                    "start_time": start_time,
                    "end_time": end_time,
                    "break_start_time": break_start,
                    "break_end_time": break_end,
                    "net_work_minutes": net,
                    "status": "confirmed",
                    "created_by": created_by,
                })

                # 체크리스트 인스턴스 자동 생성
                from app.services.checklist_instance_service import checklist_instance_service
                await checklist_instance_service.create_for_schedule(
                    db,
                    schedule_id=entry.id,
                    organization_id=organization_id,
                    store_id=req.store_id,
                    user_id=req.user_id,
                    work_date=req.work_date,
                    work_role_id=req.work_role_id,
                )

                results.append(await self._to_response(db, entry))
            await db.commit()
            return results
        except Exception:
            await db.rollback()
            raise

    async def get_entry(
        self, db: AsyncSession, entry_id: UUID, organization_id: UUID,
    ) -> ScheduleResponse:
        entry = await schedule_repository.get_by_id(db, entry_id, organization_id)
        if entry is None:
            raise NotFoundError("Schedule not found")
        return await self._to_response(db, entry)

    async def update_entry(
        self,
        db: AsyncSession,
        entry_id: UUID,
        organization_id: UUID,
        data: ScheduleUpdate,
    ) -> ScheduleResponse:
        entry = await schedule_repository.get_by_id(db, entry_id, organization_id)
        if entry is None:
            raise NotFoundError("Schedule not found")
        if entry.status == "cancelled":
            raise BadRequestError("Cancelled schedules cannot be updated")

        update_data: dict = {}
        new_user_id = entry.user_id
        new_work_date = entry.work_date
        new_start = entry.start_time
        new_end = entry.end_time
        new_break_start = entry.break_start_time
        new_break_end = entry.break_end_time

        if data.user_id is not None:
            new_user_id = UUID(data.user_id)
            update_data["user_id"] = new_user_id
        if data.work_date is not None:
            new_work_date = data.work_date
            update_data["work_date"] = new_work_date
        if data.work_role_id is not None:
            update_data["work_role_id"] = UUID(data.work_role_id) if data.work_role_id else None
        if data.start_time is not None:
            new_start = self._parse_time(data.start_time)  # type: ignore[assignment]
            update_data["start_time"] = new_start
        if data.end_time is not None:
            new_end = self._parse_time(data.end_time)  # type: ignore[assignment]
            update_data["end_time"] = new_end
        if data.break_start_time is not None:
            new_break_start = self._parse_time(data.break_start_time)
            update_data["break_start_time"] = new_break_start
        if data.break_end_time is not None:
            new_break_end = self._parse_time(data.break_end_time)
            update_data["break_end_time"] = new_break_end
        if data.note is not None:
            update_data["note"] = data.note

        if not update_data:
            return await self._to_response(db, entry)

        # Validate with new values
        validation = await self._validate_entry(
            db, new_user_id, entry.store_id, new_work_date,
            new_start, new_end, new_break_start, new_break_end,  # type: ignore[arg-type]
            data.force, exclude_id=entry.id,
        )
        if not validation.valid:
            detail = "; ".join(validation.errors + validation.warnings)
            raise BadRequestError(f"Validation failed: {detail}")

        # Recalculate net_work_minutes
        update_data["net_work_minutes"] = self._calc_net_minutes(
            new_start, new_end, new_break_start, new_break_end  # type: ignore[arg-type]
        )

        try:
            updated = await schedule_repository.update(db, entry_id, update_data, organization_id)
            if updated is None:
                raise NotFoundError("Schedule not found")
            result = await self._to_response(db, updated)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def delete_entry(
        self, db: AsyncSession, entry_id: UUID, organization_id: UUID,
    ) -> None:
        entry = await schedule_repository.get_by_id(db, entry_id, organization_id)
        if entry is None:
            raise NotFoundError("Schedule not found")
        try:
            await schedule_repository.delete(db, entry_id, organization_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def validate_entry(
        self, db: AsyncSession, organization_id: UUID, data: ScheduleCreate,
    ) -> ScheduleValidation:
        start_time = self._parse_time(data.start_time)
        end_time = self._parse_time(data.end_time)
        if start_time is None or end_time is None:
            return ScheduleValidation(valid=False, errors=["start_time and end_time are required"])
        return await self._validate_entry(
            db, UUID(data.user_id), UUID(data.store_id), data.work_date,
            start_time, end_time,
            self._parse_time(data.break_start_time),
            self._parse_time(data.break_end_time),
            data.force,
        )

    async def finalize_period_entries(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID,
        date_from: date,
        date_to: date,
        approved_by: UUID,
    ) -> FinalizeResult:
        """기간 확정 — 해당 날짜 범위의 모든 스케줄을 confirmed로 변경."""
        entries = await schedule_repository.get_by_store_date_range(db, store_id, date_from, date_to)

        created = 0
        failed = 0
        errors: list[str] = []

        for entry in entries:
            if entry.status == "cancelled":
                continue

            entry.status = "confirmed"
            entry.approved_by = approved_by
            created += 1
            await db.flush()

        return FinalizeResult(created=created, failed=failed, errors=errors)


    async def bulk_assign_checklist(
        self,
        db: AsyncSession,
        organization_id: UUID,
        schedule_ids: list[UUID],
        checklist_template_id: UUID | None,
    ) -> "BulkAssignChecklistResult":
        """스케줄 목록에 체크리스트를 일괄 할당/교체/제거합니다.

        Bulk assign, replace, or remove checklist instances for the given schedules.
        - checklist_template_id provided: create or replace cl_instance for each schedule
        - checklist_template_id is None: remove existing cl_instances for each schedule

        Validates that each schedule belongs to the current organization.
        """
        from app.models.checklist import ChecklistInstance
        from app.schemas.schedule import BulkAssignChecklistResult
        from app.services.checklist_instance_service import checklist_instance_service

        assigned = 0
        removed = 0
        skipped = 0
        errors: list[str] = []

        for schedule_id in schedule_ids:
            try:
                sched_result = await db.execute(
                    select(Schedule).where(
                        Schedule.id == schedule_id,
                        Schedule.organization_id == organization_id,
                    )
                )
                sched: Schedule | None = sched_result.scalar_one_or_none()
                if sched is None:
                    errors.append(f"Schedule {schedule_id} not found or not in org")
                    skipped += 1
                    continue

                # 기존 cl_instance 조회
                existing_result = await db.execute(
                    select(ChecklistInstance).where(
                        ChecklistInstance.schedule_id == schedule_id
                    )
                )
                existing: ChecklistInstance | None = existing_result.scalar_one_or_none()

                if checklist_template_id is None:
                    # 제거 모드 — Remove mode
                    if existing is not None:
                        await db.delete(existing)
                        await db.flush()
                        removed += 1
                    else:
                        skipped += 1
                else:
                    # 할당/교체 모드 — Assign/Replace mode
                    if existing is not None:
                        # 기존 인스턴스 교체 — Replace existing instance
                        await db.delete(existing)
                        await db.flush()

                    # 새 인스턴스 생성 — Create new instance with given template
                    # checklist_instance_service.create_for_schedule uses work_role's default_checklist_id.
                    # Here we need to create with a specific template, so we do it directly.
                    from app.models.checklist import ChecklistTemplate, ChecklistInstanceItem
                    from app.repositories.checklist_instance_repository import checklist_instance_repository
                    from sqlalchemy.orm import selectinload

                    template_result = await db.execute(
                        select(ChecklistTemplate)
                        .options(selectinload(ChecklistTemplate.items))
                        .where(ChecklistTemplate.id == checklist_template_id)
                    )
                    template: ChecklistTemplate | None = template_result.scalar_one_or_none()
                    if template is None:
                        errors.append(f"Checklist template {checklist_template_id} not found")
                        skipped += 1
                        continue

                    if not template.items:
                        errors.append(f"Template {checklist_template_id} has no items — skipping schedule {schedule_id}")
                        skipped += 1
                        continue

                    sorted_items = sorted(template.items, key=lambda x: x.sort_order)
                    instance = await checklist_instance_repository.create(
                        db,
                        {
                            "organization_id": organization_id,
                            "template_id": template.id,
                            "schedule_id": schedule_id,
                            "store_id": sched.store_id,
                            "user_id": sched.user_id,
                            "work_date": sched.work_date,
                            "total_items": len(sorted_items),
                            "completed_items": 0,
                            "status": "pending",
                        },
                    )
                    for idx, item in enumerate(sorted_items):
                        ii = ChecklistInstanceItem(
                            instance_id=instance.id,
                            item_index=idx,
                            title=item.title,
                            description=item.description,
                            verification_type=item.verification_type,
                            sort_order=item.sort_order,
                            is_completed=False,
                        )
                        db.add(ii)

                    await db.flush()
                    assigned += 1

            except Exception as exc:
                errors.append(f"Error processing schedule {schedule_id}: {exc}")
                skipped += 1

        try:
            await db.commit()
        except Exception as exc:
            await db.rollback()
            raise

        return BulkAssignChecklistResult(
            assigned=assigned,
            removed=removed,
            skipped=skipped,
            errors=errors,
        )


schedule_service: ScheduleService = ScheduleService()
