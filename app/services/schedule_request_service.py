"""스케줄 신청 서비스."""

from datetime import date as date_type, datetime, time, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization, Store
from app.models.schedule import Schedule, SchedulePeriod, ScheduleRequestTemplate, ScheduleRequestTemplateItem, StoreWorkRole
from app.models.user import User
from app.models.work import Shift, Position
from app.repositories.request_template_repository import request_template_repository
from app.repositories.schedule_repository import schedule_repository
from app.schemas.schedule import (
    RequestTemplateCreate, RequestTemplateItemResponse, RequestTemplateResponse, RequestTemplateUpdate,
    ScheduleRequestAdminCreate, ScheduleRequestAdminUpdate, ScheduleRequestCreate,
    ScheduleRequestBatchItem, ScheduleRequestBatchResult, ScheduleRequestBatchSubmit,
    ScheduleRequestResponse, ScheduleRequestUpdate, ScheduleConfirmResult,
    ScheduleConfirmPreview, ScheduleConfirmPreviewFail,
    ScheduleRequestFromTemplateResult, ScheduleRequestSkippedItem,
)
from app.utils.exceptions import BadRequestError, ForbiddenError, NotFoundError


class ScheduleRequestService:

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

    async def _resolve_hourly_rate(
        self, db: AsyncSession, user_id: UUID, store_id: UUID,
        override: float | None = None,
    ) -> float:
        """시급 cascade 결정: override > user.hourly_rate > store.default_hourly_rate > org.default_hourly_rate."""
        if override is not None:
            return override
        user_row = await db.execute(select(User.hourly_rate).where(User.id == user_id))
        user_hr = user_row.scalar()
        if user_hr is not None:
            return float(user_hr)
        store_row = await db.execute(select(Store.default_hourly_rate, Store.organization_id).where(Store.id == store_id))
        store_record = store_row.one_or_none()
        if store_record and store_record[0] is not None:
            return float(store_record[0])
        if store_record and store_record[1] is not None:
            org_row = await db.execute(select(Organization.default_hourly_rate).where(Organization.id == store_record[1]))
            org_hr = org_row.scalar()
            if org_hr is not None:
                return float(org_hr)
        return 0.0

    async def _resolve_work_role_name(self, db: AsyncSession, work_role_id) -> str | None:
        """WorkRole 이름 조회 — name이 없으면 shift·position 이름으로 fallback."""
        wr_result = await db.execute(select(StoreWorkRole).where(StoreWorkRole.id == work_role_id))
        wr = wr_result.scalar_one_or_none()
        if wr is None:
            return None
        if wr.name:
            return wr.name
        s = await db.execute(select(Shift.name).where(Shift.id == wr.shift_id))
        p = await db.execute(select(Position.name).where(Position.id == wr.position_id))
        sn = s.scalar() or ""
        pn = p.scalar() or ""
        return f"{sn} - {pn}" if sn or pn else None

    @staticmethod
    def _get_original(modifications: list | None, field: str) -> str | None:
        """Extract the first (original) value for a field from modifications JSONB."""
        if not modifications:
            return None
        for mod in modifications:
            if mod.get("field") == field:
                return mod.get("old_value")
        return None

    async def _schedule_to_request_response(self, db: AsyncSession, schedule: Schedule) -> ScheduleRequestResponse:
        """schedules 테이블의 Schedule 객체를 ScheduleRequestResponse로 변환."""
        user_result = await db.execute(select(User.full_name).where(User.id == schedule.user_id))
        user_name: str | None = user_result.scalar()

        store_result = await db.execute(select(Store.name).where(Store.id == schedule.store_id))
        store_name: str | None = store_result.scalar()

        work_role_name: str | None = None
        if schedule.work_role_id:
            work_role_name = await self._resolve_work_role_name(db, schedule.work_role_id)

        # Map schedules.status to app-expected status values
        if schedule.status == "requested" and schedule.is_modified:
            mapped_status = "modified"
        elif schedule.status == "requested":
            mapped_status = "submitted"
        else:
            mapped_status = schedule.status

        return ScheduleRequestResponse(
            id=str(schedule.id),
            user_id=str(schedule.user_id),
            user_name=user_name,
            store_id=str(schedule.store_id),
            store_name=store_name,
            work_role_id=str(schedule.work_role_id) if schedule.work_role_id else None,
            work_role_name=work_role_name,
            work_date=schedule.work_date,
            preferred_start_time=self._format_time(schedule.start_time),
            preferred_end_time=self._format_time(schedule.end_time),
            break_start_time=self._format_time(schedule.break_start_time),
            break_end_time=self._format_time(schedule.break_end_time),
            note=schedule.note,
            status=mapped_status,
            submitted_at=schedule.submitted_at or schedule.created_at,
            created_at=schedule.created_at,
            original_preferred_start_time=self._get_original(schedule.modifications, "start_time"),
            original_preferred_end_time=self._get_original(schedule.modifications, "end_time"),
            original_work_role_id=self._get_original(schedule.modifications, "work_role_id"),
            original_user_id=self._get_original(schedule.modifications, "user_id"),
            original_user_name=None,
            original_work_date=self._get_original(schedule.modifications, "work_date"),
            created_by=str(schedule.created_by) if schedule.created_by else None,
            rejection_reason=schedule.rejection_reason,
            hourly_rate=float(schedule.hourly_rate),
        )

    async def _to_template_response(
        self, db: AsyncSession, template: ScheduleRequestTemplate, items: list[ScheduleRequestTemplateItem],
    ) -> RequestTemplateResponse:
        # store_name 조회
        store_result = await db.execute(select(Store.name).where(Store.id == template.store_id))
        store_name: str | None = store_result.scalar()

        # work_role_name 일괄 조회 (name이 null이면 shift_name · position_name 조합)
        wr_ids = [item.work_role_id for item in items if item.work_role_id]
        wr_name_map: dict[str, str] = {}
        if wr_ids:
            from app.models.work import Shift, Position
            wr_result = await db.execute(
                select(StoreWorkRole.id, StoreWorkRole.name, Shift.name, Position.name)
                .join(Shift, StoreWorkRole.shift_id == Shift.id)
                .join(Position, StoreWorkRole.position_id == Position.id)
                .where(StoreWorkRole.id.in_(wr_ids))
            )
            for row in wr_result.all():
                wr_name_map[str(row[0])] = row[1] or f"{row[2]} · {row[3]}"

        return RequestTemplateResponse(
            id=str(template.id),
            user_id=str(template.user_id),
            store_id=str(template.store_id),
            name=template.name,
            is_default=template.is_default,
            items=[
                RequestTemplateItemResponse(
                    id=str(item.id),
                    template_id=str(item.template_id),
                    day_of_week=item.day_of_week,
                    work_role_id=str(item.work_role_id),
                    work_role_name=wr_name_map.get(str(item.work_role_id)),
                    store_name=store_name,
                    preferred_start_time=self._format_time(item.preferred_start_time),
                    preferred_end_time=self._format_time(item.preferred_end_time),
                )
                for item in items
            ],
            created_at=template.created_at,
            updated_at=template.updated_at,
        )

    # ─── Template CRUD ───

    async def list_all_templates(
        self, db: AsyncSession, user_id: UUID,
    ) -> list[RequestTemplateResponse]:
        templates = await request_template_repository.get_by_user(db, user_id)
        result = []
        for t in templates:
            items = await request_template_repository.get_items(db, t.id)
            result.append(await self._to_template_response(db, t, items))
        return result

    async def list_templates(
        self, db: AsyncSession, user_id: UUID, store_id: UUID,
    ) -> list[RequestTemplateResponse]:
        templates = await request_template_repository.get_by_user_store(db, user_id, store_id)
        result = []
        for t in templates:
            items = await request_template_repository.get_items(db, t.id)
            result.append(await self._to_template_response(db, t, items))
        return result

    async def create_template(
        self, db: AsyncSession, user_id: UUID, data: RequestTemplateCreate,
    ) -> RequestTemplateResponse:
        store_id = UUID(data.store_id) if data.store_id else None
        # 첫 템플릿이면 자동으로 default 설정
        is_default = data.is_default
        existing = await request_template_repository.get_by_user(db, user_id)
        if not existing:
            is_default = True
        try:
            template = await request_template_repository.create(db, {
                "user_id": user_id,
                "store_id": store_id,
                "name": data.name,
                "is_default": is_default,
            })
            items = []
            for item_data in data.items:
                item = await request_template_repository.create_item(db, {
                    "template_id": template.id,
                    "day_of_week": item_data.day_of_week,
                    "work_role_id": UUID(item_data.work_role_id),
                    "preferred_start_time": self._parse_time(item_data.preferred_start_time),
                    "preferred_end_time": self._parse_time(item_data.preferred_end_time),
                })
                items.append(item)
            result = await self._to_template_response(db, template, items)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def update_template(
        self, db: AsyncSession, template_id: UUID, user_id: UUID, data: RequestTemplateUpdate,
    ) -> RequestTemplateResponse:
        template = await request_template_repository.get_by_id(db, template_id)
        if template is None or template.user_id != user_id:
            raise NotFoundError("Template not found")

        update_data: dict = {}
        if data.name is not None:
            update_data["name"] = data.name
        if data.is_default is not None:
            update_data["is_default"] = data.is_default
        if update_data:
            await request_template_repository.update(db, template_id, update_data)

        try:
            if data.items is not None:
                await request_template_repository.delete_items(db, template_id)
                for item_data in data.items:
                    await request_template_repository.create_item(db, {
                        "template_id": template_id,
                        "day_of_week": item_data.day_of_week,
                        "work_role_id": UUID(item_data.work_role_id),
                        "preferred_start_time": self._parse_time(item_data.preferred_start_time),
                        "preferred_end_time": self._parse_time(item_data.preferred_end_time),
                    })

            updated = await request_template_repository.get_by_id(db, template_id)
            items = await request_template_repository.get_items(db, template_id)
            result = await self._to_template_response(db, updated, items)  # type: ignore[arg-type]
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def delete_template(
        self, db: AsyncSession, template_id: UUID, user_id: UUID,
    ) -> None:
        template = await request_template_repository.get_by_id(db, template_id)
        if template is None or template.user_id != user_id:
            raise NotFoundError("Template not found")
        try:
            await request_template_repository.delete(db, template_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    # ─── Request CRUD ───

    async def _find_period_for_date(
        self, db: AsyncSession, store_id: UUID, work_date: date_type,
    ) -> SchedulePeriod | None:
        """store_id + work_date로 해당 날짜를 포함하는 period 조회."""
        result = await db.execute(
            select(SchedulePeriod)
            .where(
                SchedulePeriod.store_id == store_id,
                SchedulePeriod.period_start <= work_date,
                SchedulePeriod.period_end >= work_date,
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_requests_for_user(
        self,
        db: AsyncSession,
        user_id: UUID,
        date_from: date_type | None = None,
        date_to: date_type | None = None,
    ) -> list[ScheduleRequestResponse]:
        # schedules 테이블에서 requested/rejected 상태의 스케줄 조회
        # (confirmed는 /schedules 엔드포인트에서 별도 조회)
        query = select(Schedule).where(
            Schedule.user_id == user_id,
            Schedule.status.in_(["requested", "rejected"]),
        )
        if date_from is not None:
            query = query.where(Schedule.work_date >= date_from)
        if date_to is not None:
            query = query.where(Schedule.work_date <= date_to)
        query = query.order_by(Schedule.work_date, Schedule.start_time)
        db_result = await db.execute(query)
        schedules = db_result.scalars().all()

        result = []
        for s in schedules:
            resp = await self._schedule_to_request_response(db, s)
            result.append(resp)
        return result

    async def list_requests_admin(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        date_from: date_type | None = None,
        date_to: date_type | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[ScheduleRequestResponse], int]:
        # schedules 테이블에서 requested/rejected 상태의 항목을 admin용으로 조회
        query = select(Schedule).where(
            Schedule.organization_id == organization_id,
            Schedule.status.in_(["requested", "rejected"]),
        )
        if store_id is not None:
            query = query.where(Schedule.store_id == store_id)
        if date_from is not None:
            query = query.where(Schedule.work_date >= date_from)
        if date_to is not None:
            query = query.where(Schedule.work_date <= date_to)
        query = query.order_by(Schedule.work_date, Schedule.start_time)
        schedules, total = await schedule_repository.get_paginated(db, query, page, per_page)
        responses = [await self._schedule_to_request_response(db, s) for s in schedules]
        return responses, total

    @staticmethod
    def _get_week_sunday(d: date_type) -> date_type:
        """해당 날짜가 속한 주의 일요일(주 시작일)을 반환. Sun=0 기준."""
        # isoweekday(): Mon=1 ... Sun=7
        days_since_sunday = d.isoweekday() % 7  # Sun=0, Mon=1, ..., Sat=6
        return d - timedelta(days=days_since_sunday)

    def _validate_work_date_week(self, work_date: date_type) -> None:
        """날짜 기반 주간 검증: 지난 주/이번 주 신청 불가."""
        today = datetime.now(timezone.utc).date()
        work_week_sunday = self._get_week_sunday(work_date)
        current_week_sunday = self._get_week_sunday(today)

        if work_week_sunday < current_week_sunday:
            raise BadRequestError("Cannot submit a request for a past week")
        if work_week_sunday == current_week_sunday:
            raise BadRequestError("Cannot submit a request for the current week")

    async def create_request(
        self, db: AsyncSession, user_id: UUID, data: ScheduleRequestCreate,
    ) -> ScheduleRequestResponse:
        store_id = UUID(data.store_id)

        # Period 상태 체크: period가 있고 closed면 차단, 그 외는 허용
        period = await self._find_period_for_date(db, store_id, data.work_date)
        if period is not None and period.status == "closed":
            raise BadRequestError("Request period is closed")

        work_role_id = UUID(data.work_role_id) if data.work_role_id else None

        # schedules 테이블에서 중복 신청 체크 (requested/confirmed 상태, 같은 날짜+역할)
        dup_query = select(Schedule).where(
            Schedule.user_id == user_id,
            Schedule.work_date == data.work_date,
            Schedule.status.in_(["requested", "confirmed"]),
        )
        if work_role_id is not None:
            dup_query = dup_query.where(Schedule.work_role_id == work_role_id)
        dup_result = await db.execute(dup_query)
        if dup_result.scalar_one_or_none() is not None:
            raise BadRequestError("A request with the same role already exists for this date")

        # user의 organization_id 조회 — schedules 테이블 필수 필드
        user_org_result = await db.execute(select(User.organization_id).where(User.id == user_id))
        organization_id: UUID | None = user_org_result.scalar()
        if organization_id is None:
            raise BadRequestError("User organization not found")

        # 시급 cascade 결정: user > store > org
        hourly_rate = await self._resolve_hourly_rate(db, user_id, store_id)

        start_time = self._parse_time(data.preferred_start_time)
        end_time = self._parse_time(data.preferred_end_time)

        # net_work_minutes 계산 (시간 정보가 있을 경우)
        net_minutes = 0
        if start_time is not None and end_time is not None:
            start_m = start_time.hour * 60 + start_time.minute
            end_m = end_time.hour * 60 + end_time.minute
            if end_m <= start_m:
                end_m += 24 * 60
            net_minutes = max(end_m - start_m, 0)

        try:
            schedule = await schedule_repository.create(db, {
                "organization_id": organization_id,
                "user_id": user_id,
                "store_id": store_id,
                "work_role_id": work_role_id,
                "work_date": data.work_date,
                "start_time": start_time,
                "end_time": end_time,
                "break_start_time": None,
                "break_end_time": None,
                "net_work_minutes": net_minutes,
                "note": data.note,
                "status": "requested",
                "hourly_rate": hourly_rate,
                "submitted_at": datetime.now(timezone.utc),
            })
            result = await self._schedule_to_request_response(db, schedule)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def create_requests_from_template(
        self,
        db: AsyncSession,
        user_id: UUID,
        store_id: UUID,
        date_from: date_type,
        date_to: date_type,
        template_id: UUID,
        on_conflict: str = "skip",
    ) -> ScheduleRequestFromTemplateResult:
        # schedules 테이블에 status='requested'로 생성
        period = await self._find_period_for_date(db, store_id, date_from)
        if period is not None and period.status != "open":
            raise BadRequestError("Request period is closed")

        template = await request_template_repository.get_by_id(db, template_id)
        if template is None or template.user_id != user_id:
            raise NotFoundError("Template not found")

        # user의 organization_id 조회
        user_org_result = await db.execute(select(User.organization_id).where(User.id == user_id))
        organization_id: UUID | None = user_org_result.scalar()
        if organization_id is None:
            raise BadRequestError("User organization not found")

        items = await request_template_repository.get_items(db, template_id)
        try:
            result = ScheduleRequestFromTemplateResult()
            current = date_from
            while current <= date_to:
                weekday = (current.weekday() + 1) % 7  # 0=Sun, 6=Sat
                for item in items:
                    if item.day_of_week != weekday:
                        continue
                    # 중복 체크: schedules 테이블에서 requested/confirmed 상태
                    dup_query = select(Schedule).where(
                        Schedule.user_id == user_id,
                        Schedule.work_date == current,
                        Schedule.status.in_(["requested", "confirmed"]),
                    )
                    if item.work_role_id is not None:
                        dup_query = dup_query.where(Schedule.work_role_id == item.work_role_id)
                    dup_result = await db.execute(dup_query)
                    duplicate = dup_result.scalar_one_or_none()

                    if duplicate is not None:
                        if on_conflict == "replace" and duplicate.status == "requested":
                            # 기존 requested 스케줄 시간 업데이트
                            net_minutes = 0
                            if item.preferred_start_time is not None and item.preferred_end_time is not None:
                                s_m = item.preferred_start_time.hour * 60 + item.preferred_start_time.minute
                                e_m = item.preferred_end_time.hour * 60 + item.preferred_end_time.minute
                                if e_m <= s_m:
                                    e_m += 24 * 60
                                net_minutes = max(e_m - s_m, 0)
                            updated = await schedule_repository.update(db, duplicate.id, {
                                "start_time": item.preferred_start_time,
                                "end_time": item.preferred_end_time,
                                "net_work_minutes": net_minutes,
                            })
                            result.replaced.append(await self._schedule_to_request_response(db, updated))  # type: ignore[arg-type]
                        else:
                            work_role_name = await self._resolve_work_role_name(db, item.work_role_id) if item.work_role_id else None
                            result.skipped.append(ScheduleRequestSkippedItem(
                                work_date=current,
                                work_role_id=str(item.work_role_id) if item.work_role_id else None,
                                work_role_name=work_role_name,
                                reason="이미 신청이 존재합니다" if duplicate.status != "requested" else "중복 신청",
                            ))
                    else:
                        hourly_rate = await self._resolve_hourly_rate(db, user_id, store_id)
                        net_minutes = 0
                        if item.preferred_start_time is not None and item.preferred_end_time is not None:
                            s_m = item.preferred_start_time.hour * 60 + item.preferred_start_time.minute
                            e_m = item.preferred_end_time.hour * 60 + item.preferred_end_time.minute
                            if e_m <= s_m:
                                e_m += 24 * 60
                            net_minutes = max(e_m - s_m, 0)
                        schedule = await schedule_repository.create(db, {
                            "organization_id": organization_id,
                            "user_id": user_id,
                            "store_id": store_id,
                            "work_role_id": item.work_role_id,
                            "work_date": current,
                            "start_time": item.preferred_start_time,
                            "end_time": item.preferred_end_time,
                            "net_work_minutes": net_minutes,
                            "status": "requested",
                            "hourly_rate": hourly_rate,
                            "submitted_at": datetime.now(timezone.utc),
                        })
                        result.created.append(await self._schedule_to_request_response(db, schedule))
                current += timedelta(days=1)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def copy_last_period(
        self,
        db: AsyncSession,
        user_id: UUID,
        store_id: UUID,
        date_from: date_type,
        date_to: date_type,
        on_conflict: str = "skip",
    ) -> ScheduleRequestFromTemplateResult:
        # schedules 테이블 기반으로 이전 기간 복사
        period = await self._find_period_for_date(db, store_id, date_from)
        if period is not None and period.status != "open":
            raise BadRequestError("Request period is closed")

        # user의 organization_id 조회
        user_org_result = await db.execute(select(User.organization_id).where(User.id == user_id))
        organization_id: UUID | None = user_org_result.scalar()
        if organization_id is None:
            raise BadRequestError("User organization not found")

        # 이전 주 날짜 범위 (7일 전)
        prev_date_from = date_from - timedelta(days=7)
        prev_date_to = date_to - timedelta(days=7)

        # 이전 기간 schedules 조회 (requested 상태)
        prev_result = await db.execute(
            select(Schedule).where(
                Schedule.store_id == store_id,
                Schedule.user_id == user_id,
                Schedule.work_date >= prev_date_from,
                Schedule.work_date <= prev_date_to,
                Schedule.status == "requested",
            ).order_by(Schedule.work_date)
        )
        prev_schedules = list(prev_result.scalars().all())
        if not prev_schedules:
            raise NotFoundError("No requests found in the previous period")

        # 날짜 오프셋 계산
        day_offset = (date_from - prev_date_from).days

        try:
            result = ScheduleRequestFromTemplateResult()
            for prev_s in prev_schedules:
                new_date = prev_s.work_date + timedelta(days=day_offset)
                if new_date < date_from or new_date > date_to:
                    continue
                # 중복 체크
                dup_query = select(Schedule).where(
                    Schedule.user_id == user_id,
                    Schedule.work_date == new_date,
                    Schedule.status.in_(["requested", "confirmed"]),
                )
                if prev_s.work_role_id is not None:
                    dup_query = dup_query.where(Schedule.work_role_id == prev_s.work_role_id)
                dup_result = await db.execute(dup_query)
                duplicate = dup_result.scalar_one_or_none()

                if duplicate is not None:
                    if on_conflict == "replace" and duplicate.status == "requested":
                        net_minutes = 0
                        if prev_s.start_time is not None and prev_s.end_time is not None:
                            s_m = prev_s.start_time.hour * 60 + prev_s.start_time.minute
                            e_m = prev_s.end_time.hour * 60 + prev_s.end_time.minute
                            if e_m <= s_m:
                                e_m += 24 * 60
                            net_minutes = max(e_m - s_m, 0)
                        updated = await schedule_repository.update(db, duplicate.id, {
                            "start_time": prev_s.start_time,
                            "end_time": prev_s.end_time,
                            "net_work_minutes": net_minutes,
                            "note": prev_s.note,
                        })
                        result.replaced.append(await self._schedule_to_request_response(db, updated))  # type: ignore[arg-type]
                    else:
                        work_role_name = await self._resolve_work_role_name(db, prev_s.work_role_id) if prev_s.work_role_id else None
                        result.skipped.append(ScheduleRequestSkippedItem(
                            work_date=new_date,
                            work_role_id=str(prev_s.work_role_id) if prev_s.work_role_id else None,
                            work_role_name=work_role_name,
                            reason="이미 신청이 존재합니다" if duplicate.status != "requested" else "중복 신청",
                        ))
                else:
                    hourly_rate = await self._resolve_hourly_rate(db, user_id, store_id)
                    net_minutes = 0
                    if prev_s.start_time is not None and prev_s.end_time is not None:
                        s_m = prev_s.start_time.hour * 60 + prev_s.start_time.minute
                        e_m = prev_s.end_time.hour * 60 + prev_s.end_time.minute
                        if e_m <= s_m:
                            e_m += 24 * 60
                        net_minutes = max(e_m - s_m, 0)
                    schedule = await schedule_repository.create(db, {
                        "organization_id": organization_id,
                        "user_id": user_id,
                        "store_id": store_id,
                        "work_role_id": prev_s.work_role_id,
                        "work_date": new_date,
                        "start_time": prev_s.start_time,
                        "end_time": prev_s.end_time,
                        "net_work_minutes": net_minutes,
                        "note": prev_s.note,
                        "status": "requested",
                        "hourly_rate": hourly_rate,
                        "submitted_at": datetime.now(timezone.utc),
                    })
                    result.created.append(await self._schedule_to_request_response(db, schedule))
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def update_request(
        self, db: AsyncSession, request_id: UUID, user_id: UUID, data: ScheduleRequestUpdate,
    ) -> ScheduleRequestResponse:
        # schedules 테이블에서 조회 — requested 상태이고 해당 user 소유인 경우만 수정 허용
        schedule_result = await db.execute(
            select(Schedule).where(Schedule.id == request_id, Schedule.user_id == user_id)
        )
        schedule = schedule_result.scalar_one_or_none()
        if schedule is None:
            raise NotFoundError("Request not found")
        if schedule.status != "requested":
            raise BadRequestError("Only pending requests can be updated")

        # Period 상태 체크
        work_date = data.work_date or schedule.work_date
        period = await self._find_period_for_date(db, schedule.store_id, work_date)  # type: ignore[arg-type]
        if period is not None and period.status == "closed":
            raise BadRequestError("Requests in a closed period cannot be updated")

        update_data: dict = {}
        if data.store_id is not None:
            update_data["store_id"] = UUID(data.store_id)
        if data.work_role_id is not None:
            update_data["work_role_id"] = UUID(data.work_role_id)
        if data.work_date is not None:
            update_data["work_date"] = data.work_date
        if data.preferred_start_time is not None:
            update_data["start_time"] = self._parse_time(data.preferred_start_time)
        if data.preferred_end_time is not None:
            update_data["end_time"] = self._parse_time(data.preferred_end_time)
        if data.note is not None:
            update_data["note"] = data.note

        # start_time/end_time 변경 시 net_work_minutes 재계산
        if "start_time" in update_data or "end_time" in update_data:
            new_start = update_data.get("start_time") or schedule.start_time
            new_end = update_data.get("end_time") or schedule.end_time
            if new_start is not None and new_end is not None:
                s_m = new_start.hour * 60 + new_start.minute
                e_m = new_end.hour * 60 + new_end.minute
                if e_m <= s_m:
                    e_m += 24 * 60
                update_data["net_work_minutes"] = max(e_m - s_m, 0)

        try:
            if update_data:
                updated = await schedule_repository.update(db, request_id, update_data)
            else:
                updated = schedule
            result = await self._schedule_to_request_response(db, updated)  # type: ignore[arg-type]
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def delete_request(
        self, db: AsyncSession, request_id: UUID, user_id: UUID,
    ) -> None:
        # schedules 테이블에서 조회 — requested 상태이고 해당 user 소유인 경우만 삭제 허용
        schedule_result = await db.execute(
            select(Schedule).where(Schedule.id == request_id, Schedule.user_id == user_id)
        )
        schedule = schedule_result.scalar_one_or_none()
        if schedule is None:
            raise NotFoundError("Request not found")
        if schedule.status != "requested":
            raise BadRequestError("Only pending requests can be deleted")

        # Period 상태 체크
        period = await self._find_period_for_date(db, schedule.store_id, schedule.work_date)  # type: ignore[arg-type]
        if period is not None and period.status == "closed":
            raise BadRequestError("Requests in a closed period cannot be deleted")

        try:
            await schedule_repository.delete(db, request_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def batch_submit(
        self, db: AsyncSession, user_id: UUID, data: ScheduleRequestBatchSubmit,
    ) -> ScheduleRequestBatchResult:
        """배치 제출 — 생성/수정/삭제를 한번에 처리."""
        result = ScheduleRequestBatchResult()

        for item in data.creates:
            try:
                req = await self.create_request(
                    db, user_id,
                    ScheduleRequestCreate(
                        store_id=item.store_id,
                        work_date=item.work_date,
                        work_role_id=item.work_role_id,
                        preferred_start_time=item.preferred_start_time,
                        preferred_end_time=item.preferred_end_time,
                        note=item.note,
                    ),
                )
                result.created.append(req)
            except Exception as e:
                detail = e.detail if hasattr(e, "detail") else str(e)
                result.errors.append(f"create ({item.work_date}): {detail}")

        for item in data.updates:
            try:
                req = await self.update_request(
                    db, UUID(item.id), user_id,
                    ScheduleRequestUpdate(
                        store_id=item.store_id,
                        work_role_id=item.work_role_id,
                        work_date=item.work_date,
                        preferred_start_time=item.preferred_start_time,
                        preferred_end_time=item.preferred_end_time,
                        note=item.note,
                    ),
                )
                result.updated.append(req)
            except Exception as e:
                detail = e.detail if hasattr(e, "detail") else str(e)
                result.errors.append(f"update ({item.id}): {detail}")

        for rid in data.deletes:
            try:
                await self.delete_request(db, UUID(rid), user_id)
                result.deleted_count += 1
            except Exception as e:
                detail = e.detail if hasattr(e, "detail") else str(e)
                result.errors.append(f"delete ({rid}): {detail}")

        return result

    async def update_request_status(
        self, db: AsyncSession, request_id: UUID, status: str,
        rejection_reason: str | None = None,
    ) -> ScheduleRequestResponse:
        # schedules 테이블에서 유효 상태값: requested/rejected (confirmed는 별도 confirm 흐름)
        if status not in ("requested", "rejected"):
            raise BadRequestError("Invalid status. Use: requested, rejected")
        schedule = await schedule_repository.get_by_id(db, request_id)
        if schedule is None:
            raise NotFoundError("Request not found")
        update_data: dict = {"status": status}
        if status == "rejected" and rejection_reason is not None:
            update_data["rejection_reason"] = rejection_reason
        try:
            updated = await schedule_repository.update(db, request_id, update_data)
            if updated is None:
                raise NotFoundError("Request not found")
            result = await self._schedule_to_request_response(db, updated)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    # ─── Admin: Create request ───

    async def admin_create_request(
        self, db: AsyncSession, data: ScheduleRequestAdminCreate, created_by: UUID,
    ) -> ScheduleRequestResponse:
        """Admin이 직접 request 생성 (schedules 테이블, status='requested')."""
        user_id = UUID(data.user_id)
        store_id = UUID(data.store_id)
        work_role_id = UUID(data.work_role_id) if data.work_role_id else None

        # 중복 신청 체크: schedules 테이블에서 requested/confirmed 상태
        dup_query = select(Schedule).where(
            Schedule.user_id == user_id,
            Schedule.work_date == data.work_date,
            Schedule.status.in_(["requested", "confirmed"]),
        )
        if work_role_id is not None:
            dup_query = dup_query.where(Schedule.work_role_id == work_role_id)
        dup_result = await db.execute(dup_query)
        if dup_result.scalar_one_or_none() is not None:
            raise BadRequestError("A request with the same role already exists for this date")

        # user의 organization_id 조회
        user_org_result = await db.execute(select(User.organization_id).where(User.id == user_id))
        organization_id: UUID | None = user_org_result.scalar()
        if organization_id is None:
            raise BadRequestError("User organization not found")

        # 시급 cascade 결정: override > user > store > org
        hourly_rate = await self._resolve_hourly_rate(db, user_id, store_id, data.hourly_rate)

        start_time = self._parse_time(data.preferred_start_time)
        end_time = self._parse_time(data.preferred_end_time)
        net_minutes = 0
        if start_time is not None and end_time is not None:
            s_m = start_time.hour * 60 + start_time.minute
            e_m = end_time.hour * 60 + end_time.minute
            if e_m <= s_m:
                e_m += 24 * 60
            net_minutes = max(e_m - s_m, 0)

        try:
            schedule = await schedule_repository.create(db, {
                "organization_id": organization_id,
                "user_id": user_id,
                "store_id": store_id,
                "work_role_id": work_role_id,
                "work_date": data.work_date,
                "start_time": start_time,
                "end_time": end_time,
                "break_start_time": self._parse_time(data.break_start_time),
                "break_end_time": self._parse_time(data.break_end_time),
                "net_work_minutes": net_minutes,
                "note": data.note,
                "status": "requested",
                "created_by": created_by,
                "hourly_rate": hourly_rate,
                "submitted_at": datetime.now(timezone.utc),
            })
            result = await self._schedule_to_request_response(db, schedule)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    # ─── Admin: Modify request (tracks originals via modifications JSONB) ───

    async def admin_update_request(
        self, db: AsyncSession, request_id: UUID, data: ScheduleRequestAdminUpdate,
    ) -> ScheduleRequestResponse:
        """SV/GM이 request 수정 — modifications JSONB에 원본 기록 + is_modified=True 설정."""
        schedule = await schedule_repository.get_by_id(db, request_id)
        if schedule is None:
            raise NotFoundError("Request not found")
        if schedule.status == "rejected":
            raise BadRequestError("Rejected requests cannot be updated. Revert the request first.")

        update_data: dict = {}
        has_value_change = False
        modifications: list = list(schedule.modifications or [])
        now_str = datetime.now(timezone.utc).isoformat()

        def _record_modification(field: str, old_value, new_value) -> None:
            modifications.append({
                "field": field,
                "old_value": str(old_value) if old_value is not None else None,
                "new_value": str(new_value) if new_value is not None else None,
                "modified_at": now_str,
            })

        if data.preferred_start_time is not None:
            new_time = self._parse_time(data.preferred_start_time)
            if new_time != schedule.start_time:
                _record_modification("start_time", schedule.start_time, new_time)
                update_data["start_time"] = new_time
                has_value_change = True

        if data.preferred_end_time is not None:
            new_time = self._parse_time(data.preferred_end_time)
            if new_time != schedule.end_time:
                _record_modification("end_time", schedule.end_time, new_time)
                update_data["end_time"] = new_time
                has_value_change = True

        if data.work_role_id is not None:
            new_role = UUID(data.work_role_id)
            if new_role != schedule.work_role_id:
                _record_modification("work_role_id", schedule.work_role_id, new_role)
                update_data["work_role_id"] = new_role
                has_value_change = True

        if data.user_id is not None:
            new_user = UUID(data.user_id)
            if new_user != schedule.user_id:
                _record_modification("user_id", schedule.user_id, new_user)
                update_data["user_id"] = new_user
                has_value_change = True

        if data.work_date is not None:
            if data.work_date != schedule.work_date:
                _record_modification("work_date", schedule.work_date, data.work_date)
                update_data["work_date"] = data.work_date
                has_value_change = True

        # Break time — silent update, no modify trigger. Explicit null = clear break.
        if "break_start_time" in data.model_fields_set:
            update_data["break_start_time"] = self._parse_time(data.break_start_time) if data.break_start_time else None
        if "break_end_time" in data.model_fields_set:
            update_data["break_end_time"] = self._parse_time(data.break_end_time) if data.break_end_time else None

        if data.note is not None:
            update_data["note"] = data.note

        if data.rejection_reason is not None:
            update_data["rejection_reason"] = data.rejection_reason

        # Recalculate net_work_minutes whenever start/end/break changes
        new_start = update_data.get("start_time", schedule.start_time)
        new_end = update_data.get("end_time", schedule.end_time)
        new_break_start = update_data.get("break_start_time", schedule.break_start_time)
        new_break_end = update_data.get("break_end_time", schedule.break_end_time)
        if new_start is not None and new_end is not None:
            s_m = new_start.hour * 60 + new_start.minute
            e_m = new_end.hour * 60 + new_end.minute
            if e_m <= s_m:
                e_m += 24 * 60
            net = e_m - s_m
            if new_break_start and new_break_end:
                bs = new_break_start.hour * 60 + new_break_start.minute
                be = new_break_end.hour * 60 + new_break_end.minute
                if be <= bs:
                    be += 24 * 60
                net -= (be - bs)
            update_data["net_work_minutes"] = max(net, 0)

        if has_value_change:
            update_data["modifications"] = modifications

            # Check if current values match ALL originals → auto-revert is_modified
            all_reverted = True
            if modifications:
                seen: dict[str, str | None] = {}
                for mod in modifications:
                    f = mod.get("field")
                    if f and f not in seen:
                        seen[f] = mod.get("old_value")
                for field, orig_val in seen.items():
                    current = update_data.get(field, getattr(schedule, field, None))
                    current_str = str(current) if current is not None else None
                    if current_str != orig_val:
                        all_reverted = False
                        break
            else:
                all_reverted = False

            if all_reverted:
                update_data["is_modified"] = False
                update_data["modifications"] = None
            else:
                update_data["is_modified"] = True

        if not update_data:
            return await self._schedule_to_request_response(db, schedule)

        try:
            updated = await schedule_repository.update(db, request_id, update_data)
            if updated is None:
                raise NotFoundError("Request not found")
            result = await self._schedule_to_request_response(db, updated)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    # ─── Admin: Revert request to original ───

    async def admin_revert_request(
        self, db: AsyncSession, request_id: UUID,
    ) -> ScheduleRequestResponse:
        """Modified/rejected schedule을 modifications JSONB의 최초 원본값으로 복원."""
        schedule = await schedule_repository.get_by_id(db, request_id)
        if schedule is None:
            raise NotFoundError("Request not found")
        if schedule.status not in ("requested", "rejected") and not schedule.is_modified:
            raise BadRequestError("Only modified or rejected requests can be reverted")

        revert_data: dict = {
            "status": "requested",
            "rejection_reason": None,
            "is_modified": False,
            "modifications": None,
        }

        # modifications JSONB에서 각 필드의 최초 원본값(첫 번째 기록) 복원
        if schedule.modifications:
            # 필드별 첫 번째 수정 기록만 사용 (최초 원본값)
            seen_fields: set = set()
            for mod in schedule.modifications:
                field = mod.get("field")
                if field and field not in seen_fields:
                    seen_fields.add(field)
                    old_val = mod.get("old_value")
                    if field == "start_time":
                        from datetime import time as time_type
                        revert_data["start_time"] = self._parse_time(old_val) if old_val else None
                    elif field == "end_time":
                        revert_data["end_time"] = self._parse_time(old_val) if old_val else None
                    elif field == "work_role_id":
                        revert_data["work_role_id"] = UUID(old_val) if old_val else None
                    elif field == "user_id":
                        revert_data["user_id"] = UUID(old_val) if old_val else None
                    elif field == "work_date":
                        from datetime import date as date_cls
                        revert_data["work_date"] = date_cls.fromisoformat(old_val) if old_val else None

        # net_work_minutes 재계산
        new_start = revert_data.get("start_time") or schedule.start_time
        new_end = revert_data.get("end_time") or schedule.end_time
        if new_start is not None and new_end is not None:
            s_m = new_start.hour * 60 + new_start.minute
            e_m = new_end.hour * 60 + new_end.minute
            if e_m <= s_m:
                e_m += 24 * 60
            revert_data["net_work_minutes"] = max(e_m - s_m, 0)

        try:
            updated = await schedule_repository.update(db, request_id, revert_data)
            if updated is None:
                raise NotFoundError("Request not found")
            result = await self._schedule_to_request_response(db, updated)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    # ─── Admin: Confirm requests → create schedules ───

    async def _collect_confirm_items(
        self,
        db: AsyncSession,
        store_id: UUID,
        date_from: date_type,
        date_to: date_type,
    ) -> tuple[list[Schedule], list[Schedule], list[tuple[Schedule, str]]]:
        """
        Confirm 대상 분류 (schedules 테이블에서 requested/rejected 조회).
        Returns: (to_confirm, rejected, will_fail_with_reason)
        """
        from app.models.schedule import StoreWorkRole

        db_result = await db.execute(
            select(Schedule).where(
                Schedule.store_id == store_id,
                Schedule.work_date >= date_from,
                Schedule.work_date <= date_to,
                Schedule.status.in_(["requested", "rejected"]),
            ).order_by(Schedule.work_date, Schedule.start_time)
        )
        schedules = list(db_result.scalars().all())

        to_confirm: list[Schedule] = []
        rejected: list[Schedule] = []
        will_fail: list[tuple[Schedule, str]] = []

        for s in schedules:
            if s.status == "rejected":
                rejected.append(s)
                continue

            # 시간 정보 유효성 체크 (work_role defaults로 fallback)
            start_time = s.start_time
            end_time = s.end_time

            if s.work_role_id:
                wr_result = await db.execute(
                    select(StoreWorkRole).where(StoreWorkRole.id == s.work_role_id)
                )
                wr = wr_result.scalar_one_or_none()
                if wr:
                    if start_time is None:
                        start_time = wr.default_start_time
                    if end_time is None:
                        end_time = wr.default_end_time

            if start_time is None or end_time is None:
                will_fail.append((s, "시간 정보 없음"))
            else:
                to_confirm.append(s)

        return to_confirm, rejected, will_fail

    async def confirm_requests(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID,
        date_from: date_type,
        date_to: date_type,
        confirmed_by: UUID,
    ) -> ScheduleConfirmResult:
        """requested 상태의 schedule을 confirmed로 일괄 전환 + 체크리스트 인스턴스 자동 생성.

        schedules 테이블에서 status='requested' 항목의 status만 'confirmed'으로 변경.
        새로운 schedule 행을 생성하지 않음.
        """
        from app.models.schedule import StoreWorkRole
        from app.services.checklist_instance_service import checklist_instance_service

        db_result = await db.execute(
            select(Schedule).where(
                Schedule.store_id == store_id,
                Schedule.work_date >= date_from,
                Schedule.work_date <= date_to,
                Schedule.status.in_(["requested", "rejected"]),
            ).order_by(Schedule.work_date, Schedule.start_time)
        )
        schedules = list(db_result.scalars().all())

        entries_confirmed = 0
        requests_rejected = 0
        errors: list[str] = []

        for s in schedules:
            if s.status == "rejected":
                requests_rejected += 1
                continue

            # Get time, fall back to work role defaults
            start_time = s.start_time
            end_time = s.end_time
            break_start = s.break_start_time
            break_end = s.break_end_time

            if s.work_role_id:
                wr_result = await db.execute(
                    select(StoreWorkRole).where(StoreWorkRole.id == s.work_role_id)
                )
                wr = wr_result.scalar_one_or_none()
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
                errors.append(f"Schedule {s.id}: missing time information")
                continue

            # net_work_minutes 재계산 (work_role defaults 반영)
            start_m = start_time.hour * 60 + start_time.minute
            end_m = end_time.hour * 60 + end_time.minute
            if end_m <= start_m:
                end_m += 24 * 60
            net = end_m - start_m
            if break_start and break_end:
                bs = break_start.hour * 60 + break_start.minute
                be = break_end.hour * 60 + break_end.minute
                if be <= bs:
                    be += 24 * 60
                net -= (be - bs)
            net = max(net, 0)

            try:
                # status만 confirmed로 변경 (새 행 생성 X)
                await schedule_repository.update(db, s.id, {
                    "status": "confirmed",
                    "start_time": start_time,
                    "end_time": end_time,
                    "break_start_time": break_start,
                    "break_end_time": break_end,
                    "net_work_minutes": net,
                    "approved_by": confirmed_by,
                })

                # 체크리스트 인스턴스 자동 생성 (work_role에 default_checklist가 있으면)
                await checklist_instance_service.create_for_schedule(
                    db,
                    schedule_id=s.id,
                    organization_id=organization_id,
                    store_id=s.store_id,
                    user_id=s.user_id,
                    work_date=s.work_date,
                    work_role_id=s.work_role_id,
                )

                entries_confirmed += 1
            except Exception as e:
                detail = e.detail if hasattr(e, "detail") else str(e)
                errors.append(f"Schedule {s.id}: {detail}")

        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        return ScheduleConfirmResult(
            entries_created=entries_confirmed,
            requests_confirmed=entries_confirmed,
            requests_rejected=requests_rejected,
            errors=errors,
        )

    async def preview_confirm(
        self,
        db: AsyncSession,
        store_id: UUID,
        date_from: date_type,
        date_to: date_type,
    ) -> ScheduleConfirmPreview:
        """Confirm dry-run — DB 변경 없이 결과 예측만 반환.

        schedules 테이블의 requested/rejected 항목을 기준으로 예측.
        """
        from app.models.schedule import StoreWorkRole

        db_result = await db.execute(
            select(Schedule).where(
                Schedule.store_id == store_id,
                Schedule.work_date >= date_from,
                Schedule.work_date <= date_to,
                Schedule.status.in_(["requested", "rejected"]),
            ).order_by(Schedule.work_date, Schedule.start_time)
        )
        schedules = list(db_result.scalars().all())

        will_confirm = 0
        will_skip_rejected = 0
        will_fail: list[ScheduleConfirmPreviewFail] = []

        for s in schedules:
            if s.status == "rejected":
                will_skip_rejected += 1
                continue

            start_time = s.start_time
            end_time = s.end_time

            if s.work_role_id:
                wr_result = await db.execute(
                    select(StoreWorkRole).where(StoreWorkRole.id == s.work_role_id)
                )
                wr = wr_result.scalar_one_or_none()
                if wr:
                    if start_time is None:
                        start_time = wr.default_start_time
                    if end_time is None:
                        end_time = wr.default_end_time

            if start_time is None or end_time is None:
                user_result = await db.execute(select(User.full_name).where(User.id == s.user_id))
                user_name: str | None = user_result.scalar()
                will_fail.append(ScheduleConfirmPreviewFail(
                    request_id=str(s.id),
                    user_name=user_name,
                    work_date=s.work_date,
                    reason="시간 정보 없음",
                ))
            else:
                will_confirm += 1

        return ScheduleConfirmPreview(
            will_confirm=will_confirm,
            will_skip_rejected=will_skip_rejected,
            will_fail=will_fail,
        )


schedule_request_service: ScheduleRequestService = ScheduleRequestService()
