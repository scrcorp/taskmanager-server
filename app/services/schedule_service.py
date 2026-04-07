"""스케줄 서비스 — 확정 스케줄 비즈니스 로직."""

from datetime import date, datetime, time, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization, Store
from app.models.schedule import Schedule, ScheduleAuditLog, StoreWorkRole
from app.models.user import Role, User
from app.models.work import Shift, Position
from app.repositories.break_rule_repository import break_rule_repository
from app.repositories.schedule_audit_log_repository import schedule_audit_log_repository
from app.repositories.schedule_repository import schedule_repository
from app.repositories.work_role_repository import work_role_repository
from app.schemas.schedule import (
    ScheduleAuditLogResponse, ScheduleCancel,
    ScheduleCreate, ScheduleResponse, ScheduleSwap, ScheduleUpdate,
    ScheduleValidation, FinalizeResult,
    ScheduleReject, ScheduleBulkConfirmResult,
)
from app.utils.exceptions import BadRequestError, ForbiddenError, NotFoundError


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
            work_role_name_snapshot=entry.work_role_name_snapshot,
            position_snapshot=entry.position_snapshot,
            work_date=entry.work_date,
            start_time=self._format_time(entry.start_time),  # type: ignore[arg-type]
            end_time=self._format_time(entry.end_time),  # type: ignore[arg-type]
            break_start_time=self._format_time(entry.break_start_time),
            break_end_time=self._format_time(entry.break_end_time),
            net_work_minutes=entry.net_work_minutes,
            status=entry.status,
            created_by=str(entry.created_by) if entry.created_by else None,
            approved_by=str(entry.approved_by) if entry.approved_by else None,
            confirmed_at=entry.confirmed_at,
            note=entry.note,
            hourly_rate=float(entry.hourly_rate),
            submitted_at=entry.submitted_at,
            is_modified=entry.is_modified,
            rejected_by=str(entry.rejected_by) if entry.rejected_by else None,
            rejected_at=entry.rejected_at,
            rejection_reason=entry.rejection_reason,
            cancelled_by=str(entry.cancelled_by) if entry.cancelled_by else None,
            cancelled_at=entry.cancelled_at,
            cancellation_reason=entry.cancellation_reason,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
        )

    # ─── Audit Log helpers ───────────────────────────────────────────
    async def _log_audit(
        self,
        db: AsyncSession,
        schedule_id: UUID,
        event_type: str,
        actor: User | None,
        description: str | None = None,
        reason: str | None = None,
        diff: dict[str, Any] | None = None,
    ) -> None:
        """Schedule audit log 생성. service 내에서 호출."""
        actor_role = None
        if actor and actor.role:
            actor_role = actor.role.code if hasattr(actor.role, "code") else None
        await schedule_audit_log_repository.create(
            db,
            schedule_id=schedule_id,
            event_type=event_type,
            actor_id=actor.id if actor else None,
            actor_role=actor_role,
            description=description,
            reason=reason,
            diff=diff,
        )

    @staticmethod
    def _require_gm_or_above(actor: User, action: str) -> None:
        """GM+ 권한 체크 (role.priority <= 20). 실패 시 ForbiddenError."""
        priority = actor.role.priority if actor.role else 999
        if priority > 20:
            raise ForbiddenError(f"GM or above required for action: {action}")

    async def _resolve_work_role_snapshot(
        self, db: AsyncSession, work_role_id: UUID | None,
    ) -> tuple[str | None, str | None]:
        """work_role_id로부터 (name, position_name) snapshot 추출."""
        if work_role_id is None:
            return None, None
        wr_result = await db.execute(select(StoreWorkRole).where(StoreWorkRole.id == work_role_id))
        wr = wr_result.scalar_one_or_none()
        if wr is None:
            return None, None
        # position name 조회
        pos_result = await db.execute(select(Position.name).where(Position.id == wr.position_id))
        position_name = pos_result.scalar()
        # work role name: explicit name이 있으면 그걸, 없으면 shift+position 조합
        if wr.name:
            return wr.name, position_name
        shift_result = await db.execute(select(Shift.name).where(Shift.id == wr.shift_id))
        shift_name = shift_result.scalar() or ""
        return f"{shift_name} - {position_name or ''}", position_name

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
        # status 미지정 시 cancelled 제외 (requested + confirmed 모두 반환)
        # If status not specified: exclude cancelled (return requested + confirmed)
        entries, total = await schedule_repository.get_by_filters(
            db, organization_id, store_id, user_id,
            date_from, date_to, status, page, per_page,
            sort_desc=sort_desc,
            exclude_cancelled=(status is None),
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

        # status 유효성 확인 — Validate status value (draft/requested/confirmed)
        allowed_statuses = {"draft", "requested", "confirmed"}
        entry_status = data.status if data.status in allowed_statuses else "confirmed"

        # Validate
        validation = await self._validate_entry(
            db, user_id, store_id, data.work_date,
            start_time, end_time, break_start, break_end, data.force,
        )
        if not validation.valid:
            detail = "; ".join(validation.errors + validation.warnings)
            raise BadRequestError(f"Validation failed: {detail}")

        net = self._calc_net_minutes(start_time, end_time, break_start, break_end)

        # Resolve hourly rate: provided > user.hourly_rate > store.default_hourly_rate > org.default_hourly_rate
        if data.hourly_rate is not None:
            resolved_rate = data.hourly_rate
        else:
            user_row = await db.execute(select(User.hourly_rate).where(User.id == user_id))
            user_hr = user_row.scalar()
            if user_hr is not None:
                resolved_rate = float(user_hr)
            else:
                store_row = await db.execute(select(Store.default_hourly_rate).where(Store.id == store_id))
                store_hr = store_row.scalar()
                if store_hr is not None:
                    resolved_rate = float(store_hr)
                else:
                    org_row = await db.execute(select(Organization.default_hourly_rate).where(Organization.id == organization_id))
                    org_hr = org_row.scalar()
                    resolved_rate = float(org_hr) if org_hr is not None else 0.0

        # Work Role snapshot 캡처 — name/position이 변경/삭제되어도 보존
        work_role_uuid = UUID(data.work_role_id) if data.work_role_id else None
        wr_name_snap, pos_snap = await self._resolve_work_role_snapshot(db, work_role_uuid)

        now_utc = datetime.now(timezone.utc)

        try:
            entry = await schedule_repository.create(db, {
                "organization_id": organization_id,
                "request_id": UUID(data.request_id) if data.request_id else None,
                "user_id": user_id,
                "store_id": store_id,
                "work_role_id": work_role_uuid,
                "work_role_name_snapshot": wr_name_snap,
                "position_snapshot": pos_snap,
                "work_date": data.work_date,
                "start_time": start_time,
                "end_time": end_time,
                "break_start_time": break_start,
                "break_end_time": break_end,
                "net_work_minutes": net,
                "hourly_rate": resolved_rate,
                "status": entry_status,
                "created_by": created_by,
                # requested 상태면 submitted_at 기록
                "submitted_at": now_utc if entry_status == "requested" else None,
                # confirmed 상태로 직접 생성되면 confirmed_at 기록
                "confirmed_at": now_utc if entry_status == "confirmed" else None,
                "approved_by": created_by if entry_status == "confirmed" else None,
            })

            # Audit log: 생성 이벤트
            audit_event = "created" if entry_status == "draft" else (
                "requested" if entry_status == "requested" else "confirmed"
            )
            actor = await db.scalar(select(User).where(User.id == created_by))
            await self._log_audit(
                db, entry.id, audit_event, actor,
                description=f"Schedule {audit_event} ({entry_status})",
            )

            # confirmed 상태일 때만 체크리스트 인스턴스 자동 생성
            # Only create checklist instance when status is confirmed
            if entry_status == "confirmed":
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
        """requested 상태 스케줄을 confirmed로 일괄 전환 (새 행 생성 X)."""
        from sqlalchemy import select
        from app.models.schedule import Schedule as ScheduleModel

        db_result = await db.execute(
            select(ScheduleModel).where(
                ScheduleModel.store_id == store_id,
                ScheduleModel.organization_id == organization_id,
                ScheduleModel.work_date >= date_from,
                ScheduleModel.work_date <= date_to,
                ScheduleModel.status == "requested",
            ).order_by(ScheduleModel.work_date, ScheduleModel.start_time)
        )
        pending = list(db_result.scalars().all())

        results = []
        try:
            for s in pending:
                # work_role defaults로 fallback
                start_time = s.start_time
                end_time = s.end_time
                break_start = s.break_start_time
                break_end = s.break_end_time

                if s.work_role_id:
                    wr = await work_role_repository.get_by_id(db, s.work_role_id)
                    if wr:
                        if start_time is None:
                            start_time = wr.default_start_time
                        if end_time is None:
                            end_time = wr.default_end_time
                        if break_start is None:
                            break_start = wr.break_start_time
                        if break_end is None:
                            break_end = wr.break_end_time

                if start_time is None or end_time is None:
                    continue  # 시간 정보 없으면 건너뜀

                net = self._calc_net_minutes(start_time, end_time, break_start, break_end)
                # status만 confirmed로 변경 (새 행 생성 X)
                entry = await schedule_repository.update(db, s.id, {
                    "status": "confirmed",
                    "start_time": start_time,
                    "end_time": end_time,
                    "break_start_time": break_start,
                    "break_end_time": break_end,
                    "net_work_minutes": net,
                    "approved_by": created_by,
                })

                # 체크리스트 인스턴스 자동 생성
                from app.services.checklist_instance_service import checklist_instance_service
                await checklist_instance_service.create_for_schedule(
                    db,
                    schedule_id=s.id,
                    organization_id=organization_id,
                    store_id=s.store_id,
                    user_id=s.user_id,
                    work_date=s.work_date,
                    work_role_id=s.work_role_id,
                )

                results.append(await self._to_response(db, entry))  # type: ignore[arg-type]
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
        actor: User | None = None,
    ) -> ScheduleResponse:
        entry = await schedule_repository.get_by_id(db, entry_id, organization_id)
        if entry is None:
            raise NotFoundError("Schedule not found")
        if entry.status in ("cancelled", "rejected"):
            raise BadRequestError("Cancelled or rejected schedules cannot be updated")
        # confirmed 스케줄 수정은 GM+ 권한 필요
        if entry.status == "confirmed" and actor is not None:
            self._require_gm_or_above(actor, "modify confirmed schedule")

        update_data: dict = {}
        # requested 스케줄 수정 시 변경 이력 기록용
        # Track modifications when editing a requested schedule
        modification_entries: list[dict] = []
        now_ts = datetime.now(timezone.utc).isoformat()

        new_user_id = entry.user_id
        new_work_date = entry.work_date
        new_start = entry.start_time
        new_end = entry.end_time
        new_break_start = entry.break_start_time
        new_break_end = entry.break_end_time

        if data.user_id is not None:
            new_user_id = UUID(data.user_id)
            if entry.status == "requested" and str(entry.user_id) != data.user_id:
                modification_entries.append({
                    "field": "user_id",
                    "old_value": str(entry.user_id),
                    "new_value": data.user_id,
                    "modified_at": now_ts,
                })
            update_data["user_id"] = new_user_id
        if data.work_date is not None:
            new_work_date = data.work_date
            if entry.status == "requested" and entry.work_date != data.work_date:
                modification_entries.append({
                    "field": "work_date",
                    "old_value": str(entry.work_date),
                    "new_value": str(data.work_date),
                    "modified_at": now_ts,
                })
            update_data["work_date"] = new_work_date
        if data.work_role_id is not None:
            if entry.status == "requested" and str(entry.work_role_id or "") != data.work_role_id:
                modification_entries.append({
                    "field": "work_role_id",
                    "old_value": str(entry.work_role_id) if entry.work_role_id else None,
                    "new_value": data.work_role_id,
                    "modified_at": now_ts,
                })
            update_data["work_role_id"] = UUID(data.work_role_id) if data.work_role_id else None
        if data.start_time is not None:
            new_start = self._parse_time(data.start_time)  # type: ignore[assignment]
            if entry.status == "requested" and self._format_time(entry.start_time) != data.start_time:
                modification_entries.append({
                    "field": "start_time",
                    "old_value": self._format_time(entry.start_time),
                    "new_value": data.start_time,
                    "modified_at": now_ts,
                })
            update_data["start_time"] = new_start
        if data.end_time is not None:
            new_end = self._parse_time(data.end_time)  # type: ignore[assignment]
            if entry.status == "requested" and self._format_time(entry.end_time) != data.end_time:
                modification_entries.append({
                    "field": "end_time",
                    "old_value": self._format_time(entry.end_time),
                    "new_value": data.end_time,
                    "modified_at": now_ts,
                })
            update_data["end_time"] = new_end
        # Break fields: explicitly sent null = clear break, sent value = update
        if "break_start_time" in data.model_fields_set:
            new_break_start = self._parse_time(data.break_start_time) if data.break_start_time else None
            if entry.status == "requested" and self._format_time(entry.break_start_time) != data.break_start_time:
                modification_entries.append({
                    "field": "break_start_time",
                    "old_value": self._format_time(entry.break_start_time),
                    "new_value": data.break_start_time,
                    "modified_at": now_ts,
                })
            update_data["break_start_time"] = new_break_start
        if "break_end_time" in data.model_fields_set:
            new_break_end = self._parse_time(data.break_end_time) if data.break_end_time else None
            if entry.status == "requested" and self._format_time(entry.break_end_time) != data.break_end_time:
                modification_entries.append({
                    "field": "break_end_time",
                    "old_value": self._format_time(entry.break_end_time),
                    "new_value": data.break_end_time,
                    "modified_at": now_ts,
                })
            update_data["break_end_time"] = new_break_end
        if data.note is not None:
            update_data["note"] = data.note
        if data.hourly_rate is not None:
            update_data["hourly_rate"] = data.hourly_rate

        # requested 스케줄에 변경 사항이 있으면 is_modified + modifications 업데이트
        if entry.status == "requested" and modification_entries:
            all_mods: list = (entry.modifications or []) + modification_entries
            update_data["modifications"] = all_mods

            # Check if all modified fields reverted to original → auto-clear is_modified
            seen: dict[str, str | None] = {}
            for mod in all_mods:
                f = mod.get("field")
                if f and f not in seen:
                    seen[f] = mod.get("old_value")
            all_reverted = bool(seen)
            for field, orig_val in seen.items():
                current = update_data.get(field, getattr(entry, field, None))
                if str(current) if current is not None else None != orig_val:
                    all_reverted = False
                    break
            if all_reverted:
                update_data["is_modified"] = False
                update_data["modifications"] = None
            else:
                update_data["is_modified"] = True

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
            # Audit log: build diff from modification_entries
            audit_diff: dict[str, Any] = {
                m["field"]: {"old": m.get("old_value"), "new": m.get("new_value")}
                for m in modification_entries if m.get("field")
            }
            await self._log_audit(
                db, entry_id, "modified", actor,
                description="Schedule modified",
                diff=audit_diff or None,
            )
            result = await self._to_response(db, updated)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def delete_entry(
        self, db: AsyncSession, entry_id: UUID, organization_id: UUID,
        actor: User | None = None,
    ) -> None:
        entry = await schedule_repository.get_by_id(db, entry_id, organization_id)
        if entry is None:
            raise NotFoundError("Schedule not found")
        # confirmed 스케줄 삭제는 GM+ 권한 필요
        if entry.status == "confirmed" and actor is not None:
            self._require_gm_or_above(actor, "delete confirmed schedule")
        try:
            await self._log_audit(
                db, entry_id, "deleted", actor,
                description=f"Schedule deleted from status={entry.status}",
            )
            await schedule_repository.delete(db, entry_id, organization_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def confirm_schedule(
        self,
        db: AsyncSession,
        entry_id: UUID,
        organization_id: UUID,
        approved_by: UUID,
    ) -> ScheduleResponse:
        """requested → confirmed 전환. 체크리스트 인스턴스 없으면 생성."""
        entry = await schedule_repository.get_by_id(db, entry_id, organization_id)
        if entry is None:
            raise NotFoundError("Schedule not found")
        if entry.status != "requested":
            raise BadRequestError(f"Only requested schedules can be confirmed (current status: {entry.status})")

        try:
            updated = await schedule_repository.update(
                db, entry_id,
                {
                    "status": "confirmed",
                    "approved_by": approved_by,
                    "confirmed_at": datetime.now(timezone.utc),
                },
                organization_id,
            )
            if updated is None:
                raise NotFoundError("Schedule not found")

            actor = await db.scalar(select(User).where(User.id == approved_by))
            await self._log_audit(
                db, entry_id, "confirmed", actor,
                description="Schedule confirmed",
            )

            # 체크리스트 인스턴스 없으면 생성
            # Create checklist instance if not already present
            from app.models.checklist import ChecklistInstance
            from sqlalchemy import select as sa_select
            existing = await db.execute(
                sa_select(ChecklistInstance).where(
                    ChecklistInstance.schedule_id == entry_id
                )
            )
            if existing.scalar_one_or_none() is None:
                from app.services.checklist_instance_service import checklist_instance_service
                await checklist_instance_service.create_for_schedule(
                    db,
                    schedule_id=entry_id,
                    organization_id=organization_id,
                    store_id=updated.store_id,  # type: ignore[arg-type]
                    user_id=updated.user_id,  # type: ignore[arg-type]
                    work_date=updated.work_date,
                    work_role_id=updated.work_role_id,
                )

            result = await self._to_response(db, updated)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def reject_schedule(
        self,
        db: AsyncSession,
        entry_id: UUID,
        organization_id: UUID,
        data: ScheduleReject,
        actor: User | None = None,
    ) -> ScheduleResponse:
        """requested → rejected 전환. 사유 필수."""
        entry = await schedule_repository.get_by_id(db, entry_id, organization_id)
        if entry is None:
            raise NotFoundError("Schedule not found")
        if entry.status != "requested":
            raise BadRequestError(f"Only requested schedules can be rejected (current status: {entry.status})")

        try:
            updated = await schedule_repository.update(
                db, entry_id,
                {
                    "status": "rejected",
                    "rejection_reason": data.rejection_reason,
                    "rejected_by": actor.id if actor else None,
                    "rejected_at": datetime.now(timezone.utc),
                },
                organization_id,
            )
            if updated is None:
                raise NotFoundError("Schedule not found")
            await self._log_audit(
                db, entry_id, "rejected", actor,
                description="Schedule rejected",
                reason=data.rejection_reason,
            )
            result = await self._to_response(db, updated)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    # ─── New status transition methods ───────────────────────────────

    async def submit_schedule(
        self,
        db: AsyncSession,
        entry_id: UUID,
        organization_id: UUID,
        actor: User,
    ) -> ScheduleResponse:
        """draft → requested 전환."""
        entry = await schedule_repository.get_by_id(db, entry_id, organization_id)
        if entry is None:
            raise NotFoundError("Schedule not found")
        if entry.status != "draft":
            raise BadRequestError(f"Only draft schedules can be submitted (current status: {entry.status})")
        try:
            updated = await schedule_repository.update(
                db, entry_id,
                {"status": "requested", "submitted_at": datetime.now(timezone.utc)},
                organization_id,
            )
            await self._log_audit(
                db, entry_id, "requested", actor,
                description="Schedule submitted for review",
            )
            result = await self._to_response(db, updated)  # type: ignore[arg-type]
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def revert_schedule(
        self,
        db: AsyncSession,
        entry_id: UUID,
        organization_id: UUID,
        actor: User,
    ) -> ScheduleResponse:
        """confirmed → requested 전환 (GM+ only)."""
        self._require_gm_or_above(actor, "revert confirmed schedule")
        entry = await schedule_repository.get_by_id(db, entry_id, organization_id)
        if entry is None:
            raise NotFoundError("Schedule not found")
        if entry.status != "confirmed":
            raise BadRequestError(f"Only confirmed schedules can be reverted (current status: {entry.status})")
        try:
            updated = await schedule_repository.update(
                db, entry_id,
                {
                    "status": "requested",
                    "approved_by": None,
                    "confirmed_at": None,
                },
                organization_id,
            )
            await self._log_audit(
                db, entry_id, "reverted", actor,
                description="Confirmed schedule reverted to requested",
            )
            result = await self._to_response(db, updated)  # type: ignore[arg-type]
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def cancel_schedule(
        self,
        db: AsyncSession,
        entry_id: UUID,
        organization_id: UUID,
        data: ScheduleCancel,
        actor: User,
    ) -> ScheduleResponse:
        """confirmed → cancelled 전환 (GM+ only). 사유는 권장(nullable)."""
        self._require_gm_or_above(actor, "cancel confirmed schedule")
        entry = await schedule_repository.get_by_id(db, entry_id, organization_id)
        if entry is None:
            raise NotFoundError("Schedule not found")
        if entry.status != "confirmed":
            raise BadRequestError(f"Only confirmed schedules can be cancelled (current status: {entry.status})")
        try:
            updated = await schedule_repository.update(
                db, entry_id,
                {
                    "status": "cancelled",
                    "cancelled_by": actor.id,
                    "cancelled_at": datetime.now(timezone.utc),
                    "cancellation_reason": data.cancellation_reason,
                },
                organization_id,
            )
            await self._log_audit(
                db, entry_id, "cancelled", actor,
                description="Confirmed schedule cancelled",
                reason=data.cancellation_reason,
            )
            result = await self._to_response(db, updated)  # type: ignore[arg-type]
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def swap_schedules(
        self,
        db: AsyncSession,
        entry_id: UUID,
        organization_id: UUID,
        data: ScheduleSwap,
        actor: User,
    ) -> tuple[ScheduleResponse, ScheduleResponse]:
        """두 confirmed 스케줄의 user_id를 교환 (GM+ only)."""
        self._require_gm_or_above(actor, "swap schedules")
        try:
            other_id = UUID(data.other_schedule_id)
        except ValueError:
            raise BadRequestError("Invalid other_schedule_id")
        if other_id == entry_id:
            raise BadRequestError("Cannot swap a schedule with itself")

        a = await schedule_repository.get_by_id(db, entry_id, organization_id)
        b = await schedule_repository.get_by_id(db, other_id, organization_id)
        if a is None or b is None:
            raise NotFoundError("Schedule not found")
        if a.status != "confirmed" or b.status != "confirmed":
            raise BadRequestError("Both schedules must be confirmed to swap")

        a_user = a.user_id
        b_user = b.user_id
        try:
            updated_a = await schedule_repository.update(
                db, a.id, {"user_id": b_user, "is_modified": True}, organization_id,
            )
            updated_b = await schedule_repository.update(
                db, b.id, {"user_id": a_user, "is_modified": True}, organization_id,
            )
            diff_a = {"user_id": {"old": str(a_user), "new": str(b_user)}}
            diff_b = {"user_id": {"old": str(b_user), "new": str(a_user)}}
            await self._log_audit(
                db, a.id, "swapped", actor,
                description=f"Swapped with schedule {b.id}",
                reason=data.reason, diff=diff_a,
            )
            await self._log_audit(
                db, b.id, "swapped", actor,
                description=f"Swapped with schedule {a.id}",
                reason=data.reason, diff=diff_b,
            )
            res_a = await self._to_response(db, updated_a)  # type: ignore[arg-type]
            res_b = await self._to_response(db, updated_b)  # type: ignore[arg-type]
            await db.commit()
            return res_a, res_b
        except Exception:
            await db.rollback()
            raise

    async def get_audit_log(
        self,
        db: AsyncSession,
        entry_id: UUID,
        organization_id: UUID,
    ) -> list[ScheduleAuditLogResponse]:
        """스케줄 audit log 조회 (timestamp DESC)."""
        entry = await schedule_repository.get_by_id(db, entry_id, organization_id)
        if entry is None:
            raise NotFoundError("Schedule not found")
        logs = await schedule_audit_log_repository.get_by_schedule(db, entry_id)

        # actor 이름 batch 조회
        actor_ids = {l.actor_id for l in logs if l.actor_id is not None}
        actor_names: dict[UUID, str] = {}
        if actor_ids:
            users_result = await db.execute(
                select(User.id, User.full_name).where(User.id.in_(actor_ids))
            )
            actor_names = {row[0]: row[1] for row in users_result.all()}

        return [
            ScheduleAuditLogResponse(
                id=str(l.id),
                schedule_id=str(l.schedule_id),
                event_type=l.event_type,
                actor_id=str(l.actor_id) if l.actor_id else None,
                actor_name=actor_names.get(l.actor_id) if l.actor_id else None,
                actor_role=l.actor_role,
                timestamp=l.timestamp,
                description=l.description,
                reason=l.reason,
                diff=l.diff,
            )
            for l in logs
        ]

    async def bulk_confirm(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID,
        date_from: date,
        date_to: date,
        approved_by: UUID,
    ) -> ScheduleBulkConfirmResult:
        """기간 내 모든 requested 스케줄을 일괄 confirmed로 전환.

        같은 user+date에 이미 confirmed 스케줄이 있으면 겹침 여부를 확인하고 건너뜀.
        """
        # 해당 기간의 모든 requested 스케줄 조회
        # Fetch all requested schedules in the date range
        entries_result = await db.execute(
            select(Schedule).where(
                Schedule.organization_id == organization_id,
                Schedule.store_id == store_id,
                Schedule.work_date >= date_from,
                Schedule.work_date <= date_to,
                Schedule.status == "requested",
            )
        )
        entries = list(entries_result.scalars().all())

        confirmed = 0
        skipped = 0
        errors: list[str] = []

        try:
            for entry in entries:
                # 같은 user+date에 이미 confirmed 스케줄이 있으면 건너뜀 (시간 겹침 체크)
                # Skip if there is already a confirmed schedule for same user+date with overlapping time
                if entry.start_time and entry.end_time:
                    start_m = self._time_to_minutes(entry.start_time)
                    end_m = self._time_to_minutes(entry.end_time)
                    overlap = await schedule_repository.check_time_overlap(
                        db, entry.user_id, entry.work_date, start_m, end_m, exclude_id=entry.id  # type: ignore[arg-type]
                    )
                    if overlap:
                        skipped += 1
                        errors.append(
                            f"Schedule {entry.id} skipped: time overlap with existing confirmed schedule"
                        )
                        continue

                entry.status = "confirmed"
                entry.approved_by = approved_by
                await db.flush()

                # 체크리스트 인스턴스 없으면 생성
                # Create checklist instance if not already present
                from app.models.checklist import ChecklistInstance
                existing_ci = await db.execute(
                    select(ChecklistInstance).where(
                        ChecklistInstance.schedule_id == entry.id
                    )
                )
                if existing_ci.scalar_one_or_none() is None:
                    from app.services.checklist_instance_service import checklist_instance_service
                    await checklist_instance_service.create_for_schedule(
                        db,
                        schedule_id=entry.id,
                        organization_id=organization_id,
                        store_id=entry.store_id,  # type: ignore[arg-type]
                        user_id=entry.user_id,  # type: ignore[arg-type]
                        work_date=entry.work_date,
                        work_role_id=entry.work_role_id,
                    )

                confirmed += 1

            await db.commit()
        except Exception:
            await db.rollback()
            raise

        return ScheduleBulkConfirmResult(confirmed=confirmed, skipped=skipped, errors=errors)

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
