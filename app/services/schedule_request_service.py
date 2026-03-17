"""스케줄 신청 서비스."""

from datetime import date as date_type, datetime, time, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Store
from app.models.schedule import SchedulePeriod, ScheduleRequest, ScheduleRequestTemplate, ScheduleRequestTemplateItem, StoreWorkRole
from app.models.user import User
from app.models.work import Shift, Position
from app.repositories.request_template_repository import request_template_repository
from app.repositories.schedule_request_repository import schedule_request_repository
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

    async def _to_request_response(self, db: AsyncSession, req: ScheduleRequest) -> ScheduleRequestResponse:
        user_result = await db.execute(select(User.full_name).where(User.id == req.user_id))
        user_name: str | None = user_result.scalar()

        store_result = await db.execute(select(Store.name).where(Store.id == req.store_id))
        store_name: str | None = store_result.scalar()

        work_role_name: str | None = None
        if req.work_role_id:
            work_role_name = await self._resolve_work_role_name(db, req.work_role_id)

        # Original user name (for modified display)
        original_user_name: str | None = None
        if req.original_user_id:
            ou_result = await db.execute(select(User.full_name).where(User.id == req.original_user_id))
            original_user_name = ou_result.scalar()

        return ScheduleRequestResponse(
            id=str(req.id),
            user_id=str(req.user_id),
            user_name=user_name,
            store_id=str(req.store_id),
            store_name=store_name,
            work_role_id=str(req.work_role_id) if req.work_role_id else None,
            work_role_name=work_role_name,
            work_date=req.work_date,
            preferred_start_time=self._format_time(req.preferred_start_time),
            preferred_end_time=self._format_time(req.preferred_end_time),
            break_start_time=self._format_time(req.break_start_time),
            break_end_time=self._format_time(req.break_end_time),
            note=req.note,
            status=req.status,
            submitted_at=req.submitted_at,
            created_at=req.created_at,
            original_preferred_start_time=self._format_time(req.original_preferred_start_time),
            original_preferred_end_time=self._format_time(req.original_preferred_end_time),
            original_work_role_id=str(req.original_work_role_id) if req.original_work_role_id else None,
            original_user_id=str(req.original_user_id) if req.original_user_id else None,
            original_user_name=original_user_name,
            original_work_date=req.original_work_date,
            created_by=str(req.created_by) if req.created_by else None,
            rejection_reason=req.rejection_reason,
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
        requests, _ = await schedule_request_repository.get_by_filters(
            db, user_id=user_id,
            date_from=date_from, date_to=date_to, per_page=200,
        )
        # Staff 에게는 admin 수정 내역 숨김:
        # admin이 생성한 request (created_by != null)는 제외
        result = []
        for r in requests:
            if r.created_by is not None:
                continue  # admin-created 제외
            resp = await self._to_staff_request_response(db, r)
            result.append(resp)
        return result

    async def _to_staff_request_response(self, db: AsyncSession, req: ScheduleRequest) -> ScheduleRequestResponse:
        """Staff용 response — 실제 상태와 변경 내역을 표시."""
        user_result = await db.execute(select(User.full_name).where(User.id == req.user_id))
        user_name: str | None = user_result.scalar()

        store_result = await db.execute(select(Store.name).where(Store.id == req.store_id))
        store_name: str | None = store_result.scalar()

        work_role_name: str | None = None
        if req.work_role_id:
            work_role_name = await self._resolve_work_role_name(db, req.work_role_id)

        return ScheduleRequestResponse(
            id=str(req.id),
            user_id=str(req.user_id),
            user_name=user_name,
            store_id=str(req.store_id),
            store_name=store_name,
            work_role_id=str(req.work_role_id) if req.work_role_id else None,
            work_role_name=work_role_name,
            work_date=req.work_date,
            preferred_start_time=self._format_time(req.preferred_start_time),
            preferred_end_time=self._format_time(req.preferred_end_time),
            note=req.note,
            status=req.status,
            submitted_at=req.submitted_at,
            created_at=req.created_at,
            original_preferred_start_time=self._format_time(req.original_preferred_start_time) if req.original_preferred_start_time else None,
            original_preferred_end_time=self._format_time(req.original_preferred_end_time) if req.original_preferred_end_time else None,
            original_work_role_id=str(req.original_work_role_id) if req.original_work_role_id else None,
            original_user_id=None,
            original_user_name=None,
            original_work_date=str(req.original_work_date) if req.original_work_date else None,
            created_by=None,
            rejection_reason=req.rejection_reason,
        )

    async def list_requests_admin(
        self,
        db: AsyncSession,
        store_id: UUID | None = None,
        date_from: date_type | None = None,
        date_to: date_type | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[ScheduleRequestResponse], int]:
        requests, total = await schedule_request_repository.get_by_filters(
            db, store_id=store_id,
            date_from=date_from, date_to=date_to,
            page=page, per_page=per_page,
        )
        responses = [await self._to_request_response(db, r) for r in requests]
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

        # Period 상태 체크: store_id + work_date로 period lookup
        period = await self._find_period_for_date(db, store_id, data.work_date)
        if period is not None:
            if period.status != "open":
                raise BadRequestError("Request period is closed")
        else:
            # Period 없으면 날짜 기반 검증
            self._validate_work_date_week(data.work_date)

        # 중복 신청 체크 (rejected 제외)
        work_role_id = UUID(data.work_role_id) if data.work_role_id else None
        duplicate = await schedule_request_repository.find_active_duplicate(
            db, user_id, data.work_date, work_role_id
        )
        if duplicate is not None:
            raise BadRequestError("A request with the same role already exists for this date")

        try:
            req = await schedule_request_repository.create(db, {
                "user_id": user_id,
                "store_id": store_id,
                "work_role_id": UUID(data.work_role_id) if data.work_role_id else None,
                "work_date": data.work_date,
                "preferred_start_time": self._parse_time(data.preferred_start_time),
                "preferred_end_time": self._parse_time(data.preferred_end_time),
                "note": data.note,
                "status": "submitted",
            })
            result = await self._to_request_response(db, req)
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
        # Period 상태 체크
        period = await self._find_period_for_date(db, store_id, date_from)
        if period is not None and period.status != "open":
            raise BadRequestError("Request period is closed")

        template = await request_template_repository.get_by_id(db, template_id)
        if template is None or template.user_id != user_id:
            raise NotFoundError("Template not found")

        items = await request_template_repository.get_items(db, template_id)
        try:
            result = ScheduleRequestFromTemplateResult()
            current = date_from
            while current <= date_to:
                weekday = (current.weekday() + 1) % 7  # 0=Sun, 6=Sat
                for item in items:
                    if item.day_of_week != weekday:
                        continue
                    duplicate = await schedule_request_repository.find_active_duplicate(
                        db, user_id, current, item.work_role_id
                    )
                    if duplicate is not None:
                        if on_conflict == "replace" and duplicate.status == "submitted":
                            # 기존 submitted request 업데이트
                            updated = await schedule_request_repository.update(db, duplicate.id, {
                                "preferred_start_time": item.preferred_start_time,
                                "preferred_end_time": item.preferred_end_time,
                            })
                            result.replaced.append(await self._to_request_response(db, updated))  # type: ignore[arg-type]
                        else:
                            work_role_name = await self._resolve_work_role_name(db, item.work_role_id) if item.work_role_id else None
                            result.skipped.append(ScheduleRequestSkippedItem(
                                work_date=current,
                                work_role_id=str(item.work_role_id) if item.work_role_id else None,
                                work_role_name=work_role_name,
                                reason="이미 신청이 존재합니다" if duplicate.status != "submitted" else "중복 신청",
                            ))
                    else:
                        req = await schedule_request_repository.create(db, {
                            "user_id": user_id,
                            "store_id": store_id,
                            "work_role_id": item.work_role_id,
                            "work_date": current,
                            "preferred_start_time": item.preferred_start_time,
                            "preferred_end_time": item.preferred_end_time,
                            "status": "submitted",
                        })
                        result.created.append(await self._to_request_response(db, req))
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
        # 대상 기간 상태 체크
        period = await self._find_period_for_date(db, store_id, date_from)
        if period is not None and period.status != "open":
            raise BadRequestError("Request period is closed")

        # 이전 주 날짜 범위 (7일 전)
        prev_date_from = date_from - timedelta(days=7)
        prev_date_to = date_to - timedelta(days=7)

        prev_requests = await schedule_request_repository.get_by_store_date_range_user(
            db, store_id, user_id, prev_date_from, prev_date_to
        )
        if not prev_requests:
            raise NotFoundError("No requests found in the previous period")

        # 날짜 오프셋 계산
        day_offset = (date_from - prev_date_from).days

        try:
            result = ScheduleRequestFromTemplateResult()
            for prev_req in prev_requests:
                new_date = prev_req.work_date + timedelta(days=day_offset)
                if new_date < date_from or new_date > date_to:
                    continue
                duplicate = await schedule_request_repository.find_active_duplicate(
                    db, user_id, new_date, prev_req.work_role_id
                )
                if duplicate is not None:
                    if on_conflict == "replace" and duplicate.status == "submitted":
                        updated = await schedule_request_repository.update(db, duplicate.id, {
                            "preferred_start_time": prev_req.preferred_start_time,
                            "preferred_end_time": prev_req.preferred_end_time,
                            "note": prev_req.note,
                        })
                        result.replaced.append(await self._to_request_response(db, updated))  # type: ignore[arg-type]
                    else:
                        work_role_name = await self._resolve_work_role_name(db, prev_req.work_role_id) if prev_req.work_role_id else None
                        result.skipped.append(ScheduleRequestSkippedItem(
                            work_date=new_date,
                            work_role_id=str(prev_req.work_role_id) if prev_req.work_role_id else None,
                            work_role_name=work_role_name,
                            reason="이미 신청이 존재합니다" if duplicate.status != "submitted" else "중복 신청",
                        ))
                else:
                    req = await schedule_request_repository.create(db, {
                        "user_id": user_id,
                        "store_id": store_id,
                        "work_role_id": prev_req.work_role_id,
                        "work_date": new_date,
                        "preferred_start_time": prev_req.preferred_start_time,
                        "preferred_end_time": prev_req.preferred_end_time,
                        "note": prev_req.note,
                        "status": "submitted",
                    })
                    result.created.append(await self._to_request_response(db, req))
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def update_request(
        self, db: AsyncSession, request_id: UUID, user_id: UUID, data: ScheduleRequestUpdate,
    ) -> ScheduleRequestResponse:
        req = await schedule_request_repository.get_by_id(db, request_id)
        if req is None or req.user_id != user_id:
            raise NotFoundError("Request not found")
        if req.status not in ("submitted",):
            raise BadRequestError("Approved or rejected requests cannot be updated")

        # Period 상태 체크: store_id + work_date로 lookup
        work_date = data.work_date or req.work_date
        period = await self._find_period_for_date(db, req.store_id, work_date)
        if period is not None:
            if period.status != "open":
                raise BadRequestError("Requests in a closed period cannot be updated")
        else:
            self._validate_work_date_week(work_date)

        update_data: dict = {}
        if data.store_id is not None:
            update_data["store_id"] = UUID(data.store_id)
        if data.work_role_id is not None:
            update_data["work_role_id"] = UUID(data.work_role_id)
        if data.work_date is not None:
            update_data["work_date"] = data.work_date
        if data.preferred_start_time is not None:
            update_data["preferred_start_time"] = self._parse_time(data.preferred_start_time)
        if data.preferred_end_time is not None:
            update_data["preferred_end_time"] = self._parse_time(data.preferred_end_time)
        if data.note is not None:
            update_data["note"] = data.note

        try:
            if update_data:
                updated = await schedule_request_repository.update(db, request_id, update_data)
            else:
                updated = req
            result = await self._to_request_response(db, updated)  # type: ignore[arg-type]
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def delete_request(
        self, db: AsyncSession, request_id: UUID, user_id: UUID,
    ) -> None:
        req = await schedule_request_repository.get_by_id(db, request_id)
        if req is None or req.user_id != user_id:
            raise NotFoundError("Request not found")

        # Period 상태 체크: store_id + work_date로 lookup
        period = await self._find_period_for_date(db, req.store_id, req.work_date)
        if period is not None:
            if period.status != "open":
                raise BadRequestError("Requests in a closed period cannot be deleted")
        else:
            self._validate_work_date_week(req.work_date)

        try:
            await schedule_request_repository.delete(db, request_id)
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
        if status not in ("accepted", "modified", "rejected"):
            raise BadRequestError("Invalid status. Use: accepted, modified, rejected")
        req = await schedule_request_repository.get_by_id(db, request_id)
        if req is None:
            raise NotFoundError("Request not found")
        update_data: dict = {"status": status}
        if status == "rejected" and rejection_reason is not None:
            update_data["rejection_reason"] = rejection_reason
        try:
            updated = await schedule_request_repository.update(db, request_id, update_data)
            if updated is None:
                raise NotFoundError("Request not found")
            result = await self._to_request_response(db, updated)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    # ─── Admin: Create request ───

    async def admin_create_request(
        self, db: AsyncSession, data: ScheduleRequestAdminCreate, created_by: UUID,
    ) -> ScheduleRequestResponse:
        """Admin이 직접 request 생성 (staff에게 안 보임, confirm 시 entry로 변환)."""
        # 중복 신청 체크 (rejected 제외)
        work_role_id = UUID(data.work_role_id) if data.work_role_id else None
        duplicate = await schedule_request_repository.find_active_duplicate(
            db, UUID(data.user_id), data.work_date, work_role_id
        )
        if duplicate is not None:
            raise BadRequestError("A request with the same role already exists for this date")

        try:
            req = await schedule_request_repository.create(db, {
                "user_id": UUID(data.user_id),
                "store_id": UUID(data.store_id),
                "work_role_id": UUID(data.work_role_id) if data.work_role_id else None,
                "work_date": data.work_date,
                "preferred_start_time": self._parse_time(data.preferred_start_time),
                "preferred_end_time": self._parse_time(data.preferred_end_time),
                "break_start_time": self._parse_time(data.break_start_time),
                "break_end_time": self._parse_time(data.break_end_time),
                "note": data.note,
                "status": "submitted",
                "created_by": created_by,
            })
            result = await self._to_request_response(db, req)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    # ─── Admin: Modify request (tracks originals) ───

    async def admin_update_request(
        self, db: AsyncSession, request_id: UUID, data: ScheduleRequestAdminUpdate,
    ) -> ScheduleRequestResponse:
        """SV/GM이 request 수정 — 원본 저장 + status=modified 자동 설정."""
        req = await schedule_request_repository.get_by_id(db, request_id)
        if req is None:
            raise NotFoundError("Request not found")
        if req.status == "rejected":
            raise BadRequestError("Rejected requests cannot be updated. Revert the request first.")

        update_data: dict = {}
        has_value_change = False

        # Track originals on first change per field (regardless of current status)
        if data.preferred_start_time is not None:
            new_time = self._parse_time(data.preferred_start_time)
            if new_time != req.preferred_start_time:
                if req.original_preferred_start_time is None:
                    update_data["original_preferred_start_time"] = req.preferred_start_time
                    update_data["original_preferred_end_time"] = req.preferred_end_time
                update_data["preferred_start_time"] = new_time
                has_value_change = True

        if data.preferred_end_time is not None:
            new_time = self._parse_time(data.preferred_end_time)
            if new_time != req.preferred_end_time:
                if req.original_preferred_start_time is None:
                    update_data.setdefault("original_preferred_start_time", req.preferred_start_time)
                    update_data.setdefault("original_preferred_end_time", req.preferred_end_time)
                update_data["preferred_end_time"] = new_time
                has_value_change = True

        if data.work_role_id is not None:
            new_role = UUID(data.work_role_id)
            if new_role != req.work_role_id:
                if req.original_work_role_id is None:
                    update_data["original_work_role_id"] = req.work_role_id
                update_data["work_role_id"] = new_role
                has_value_change = True

        if data.user_id is not None:
            new_user = UUID(data.user_id)
            if new_user != req.user_id:
                if req.original_user_id is None:
                    update_data["original_user_id"] = req.user_id
                update_data["user_id"] = new_user
                has_value_change = True

        if data.work_date is not None:
            if data.work_date != req.work_date:
                if req.original_work_date is None:
                    update_data["original_work_date"] = req.work_date
                update_data["work_date"] = data.work_date
                has_value_change = True

        # Break time — silent update, no modify trigger
        if data.break_start_time is not None:
            update_data["break_start_time"] = self._parse_time(data.break_start_time)
        if data.break_end_time is not None:
            update_data["break_end_time"] = self._parse_time(data.break_end_time)

        if data.note is not None:
            update_data["note"] = data.note

        if data.rejection_reason is not None:
            update_data["rejection_reason"] = data.rejection_reason

        if has_value_change:
            update_data["status"] = "modified"

        if not update_data:
            return await self._to_request_response(db, req)

        try:
            # Auto-unmodify: if all values match originals, revert status
            updated = await schedule_request_repository.update(db, request_id, update_data)
            if updated is None:
                raise NotFoundError("Request not found")

            # Check auto-unmodify after update
            await self._auto_unmodify(db, updated)
            # Re-fetch after potential unmodify
            final = await schedule_request_repository.get_by_id(db, request_id)
            result = await self._to_request_response(db, final)  # type: ignore[arg-type]
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def _auto_unmodify(self, db: AsyncSession, req: ScheduleRequest) -> None:
        """수정된 값이 모두 원래 값과 일치하면 자동으로 submitted로 복원."""
        if req.status != "modified":
            return
        time_match = (
            req.original_preferred_start_time is None
            or (req.preferred_start_time == req.original_preferred_start_time
                and req.preferred_end_time == req.original_preferred_end_time)
        )
        role_match = req.original_work_role_id is None or req.work_role_id == req.original_work_role_id
        user_match = req.original_user_id is None or req.user_id == req.original_user_id
        date_match = req.original_work_date is None or req.work_date == req.original_work_date

        if time_match and role_match and user_match and date_match:
            await schedule_request_repository.update(db, req.id, {
                "status": "submitted",
                "original_preferred_start_time": None,
                "original_preferred_end_time": None,
                "original_work_role_id": None,
                "original_user_id": None,
                "original_work_date": None,
            })

    # ─── Admin: Revert request to original ───

    async def admin_revert_request(
        self, db: AsyncSession, request_id: UUID,
    ) -> ScheduleRequestResponse:
        """Modified/rejected request를 원래 값으로 복원."""
        req = await schedule_request_repository.get_by_id(db, request_id)
        if req is None:
            raise NotFoundError("Request not found")
        if req.status not in ("modified", "rejected"):
            raise BadRequestError("Only modified or rejected requests can be reverted")

        revert_data: dict = {"status": "submitted", "rejection_reason": None}
        if req.original_preferred_start_time is not None:
            revert_data["preferred_start_time"] = req.original_preferred_start_time
            revert_data["preferred_end_time"] = req.original_preferred_end_time
        if req.original_work_role_id is not None:
            revert_data["work_role_id"] = req.original_work_role_id
        if req.original_user_id is not None:
            revert_data["user_id"] = req.original_user_id
        if req.original_work_date is not None:
            revert_data["work_date"] = req.original_work_date
        # Clear originals
        revert_data["original_preferred_start_time"] = None
        revert_data["original_preferred_end_time"] = None
        revert_data["original_work_role_id"] = None
        revert_data["original_user_id"] = None
        revert_data["original_work_date"] = None

        try:
            updated = await schedule_request_repository.update(db, request_id, revert_data)
            if updated is None:
                raise NotFoundError("Request not found")
            result = await self._to_request_response(db, updated)
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
    ) -> tuple[list[ScheduleRequest], list[ScheduleRequest], list[tuple[ScheduleRequest, str]]]:
        """
        Confirm 대상 분류.
        Returns: (to_confirm, rejected, will_fail_with_reason)
        """
        from app.models.schedule import StoreWorkRole

        requests, _ = await schedule_request_repository.get_by_filters(
            db, store_id=store_id, date_from=date_from, date_to=date_to, per_page=500,
        )

        to_confirm: list[ScheduleRequest] = []
        rejected: list[ScheduleRequest] = []
        will_fail: list[tuple[ScheduleRequest, str]] = []

        for req in requests:
            if req.status == "rejected":
                rejected.append(req)
                continue

            # 시간 정보 유효성 체크
            start_time = req.preferred_start_time
            end_time = req.preferred_end_time

            if req.work_role_id:
                wr_result = await db.execute(
                    select(StoreWorkRole).where(StoreWorkRole.id == req.work_role_id)
                )
                wr = wr_result.scalar_one_or_none()
                if wr:
                    if start_time is None:
                        start_time = wr.default_start_time
                    if end_time is None:
                        end_time = wr.default_end_time

            if start_time is None or end_time is None:
                will_fail.append((req, "시간 정보 없음"))
            else:
                to_confirm.append(req)

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
        """Non-rejected request를 schedule로 일괄 변환 + 체크리스트 인스턴스 자동 생성."""
        from app.models.schedule import StoreWorkRole
        from app.repositories.schedule_repository import schedule_repository
        from app.services.checklist_instance_service import checklist_instance_service

        requests, _ = await schedule_request_repository.get_by_filters(
            db, store_id=store_id, date_from=date_from, date_to=date_to, per_page=500,
        )

        entries_created = 0
        requests_confirmed = 0
        requests_rejected = 0
        errors: list[str] = []

        for req in requests:
            if req.status == "rejected":
                requests_rejected += 1
                continue

            # Get time from request, then fall back to work role defaults
            start_time = req.preferred_start_time
            end_time = req.preferred_end_time
            break_start = req.break_start_time
            break_end = req.break_end_time

            if req.work_role_id:
                wr_result = await db.execute(
                    select(StoreWorkRole).where(StoreWorkRole.id == req.work_role_id)
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
                errors.append(f"Request {req.id}: 시간 정보 없음")
                continue

            # Calculate net minutes
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
                schedule = await schedule_repository.create(db, {
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
                    "created_by": confirmed_by,
                    "approved_by": confirmed_by,
                })

                # 체크리스트 인스턴스 자동 생성 (work_role에 default_checklist가 있으면)
                await checklist_instance_service.create_for_schedule(
                    db,
                    schedule_id=schedule.id,
                    organization_id=organization_id,
                    store_id=req.store_id,
                    user_id=req.user_id,
                    work_date=req.work_date,
                    work_role_id=req.work_role_id,
                )

                # Update request status to accepted
                await schedule_request_repository.update(db, req.id, {"status": "accepted"})
                entries_created += 1
                requests_confirmed += 1
            except Exception as e:
                detail = e.detail if hasattr(e, "detail") else str(e)
                errors.append(f"Request {req.id}: {detail}")

        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        return ScheduleConfirmResult(
            entries_created=entries_created,
            requests_confirmed=requests_confirmed,
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
        """Confirm dry-run — DB 변경 없이 결과 예측만 반환 (S1)."""
        from app.models.schedule import StoreWorkRole

        requests, _ = await schedule_request_repository.get_by_filters(
            db, store_id=store_id, date_from=date_from, date_to=date_to, per_page=500,
        )

        will_confirm = 0
        will_skip_rejected = 0
        will_fail: list[ScheduleConfirmPreviewFail] = []

        for req in requests:
            if req.status == "rejected":
                will_skip_rejected += 1
                continue

            start_time = req.preferred_start_time
            end_time = req.preferred_end_time

            if req.work_role_id:
                wr_result = await db.execute(
                    select(StoreWorkRole).where(StoreWorkRole.id == req.work_role_id)
                )
                wr = wr_result.scalar_one_or_none()
                if wr:
                    if start_time is None:
                        start_time = wr.default_start_time
                    if end_time is None:
                        end_time = wr.default_end_time

            if start_time is None or end_time is None:
                user_result = await db.execute(select(User.full_name).where(User.id == req.user_id))
                user_name: str | None = user_result.scalar()
                will_fail.append(ScheduleConfirmPreviewFail(
                    request_id=str(req.id),
                    user_name=user_name,
                    work_date=req.work_date,
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
