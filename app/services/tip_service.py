"""팁 서비스 — Tip business logic.

직원용 흐름만 (Stage A):
    - create_entry: 본인 entry 생성 + nested distributions 동시 생성 + audit log
    - update_entry: 본인 entry 수정 (확정 전; Stage B 에서 lock)
    - list_my_entries: 본인 일별 entries
    - list_incoming_distributions: 본인이 받은 분배
    - accept_distribution: OK 처리 + audit log

매니저용 흐름은 Stage B 에서 추가 예정.
"""

from __future__ import annotations

from datetime import date as DateType, datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import calendar

from app.models.alert import Alert
from app.models.organization import Store
from app.models.schedule import Schedule, StoreWorkRole
from app.models.tip import (
    Form4070Document, TipAuditLog, TipDistribution, TipEntry, TipPeriod,
)
from app.models.user import User
from app.schemas.tip import (
    TipDistributionCreate,
    TipEntryCreate,
    TipEntryUpdate,
)
from app.utils.exceptions import (
    BadRequestError,
    ForbiddenError,
    NotFoundError,
)

# 자동 수락 타이머 — 가이드 결정사항 1.6: 24h 단순 처리 (Stage A)
AUTO_ACCEPT_HOURS = 24


def cycle_for_date(d: DateType) -> tuple[DateType, DateType]:
    """주어진 날짜가 속한 반월 사이클의 (start, end). 1~15 또는 16~말일."""
    if d.day <= 15:
        return DateType(d.year, d.month, 1), DateType(d.year, d.month, 15)
    last_day = calendar.monthrange(d.year, d.month)[1]
    return DateType(d.year, d.month, 16), DateType(d.year, d.month, last_day)


class TipService:

    # ── Period 헬퍼 ────────────────────────────────────────────

    async def get_or_create_period(
        self,
        db: AsyncSession,
        *,
        store_id: UUID,
        date_in_cycle: DateType,
    ) -> TipPeriod:
        """date 가 속한 사이클의 period 를 가져오거나 새로 만든다."""
        start, end = cycle_for_date(date_in_cycle)
        existing = await db.scalar(
            select(TipPeriod).where(
                TipPeriod.store_id == store_id,
                TipPeriod.start_date == start,
                TipPeriod.end_date == end,
            )
        )
        if existing is not None:
            return existing
        period = TipPeriod(
            store_id=store_id,
            start_date=start,
            end_date=end,
            status="open",
        )
        db.add(period)
        await db.flush()
        return period

    async def is_period_confirmed(
        self,
        db: AsyncSession,
        *,
        store_id: UUID,
        date_in_cycle: DateType,
    ) -> bool:
        start, end = cycle_for_date(date_in_cycle)
        period = await db.scalar(
            select(TipPeriod).where(
                TipPeriod.store_id == store_id,
                TipPeriod.start_date == start,
                TipPeriod.end_date == end,
            )
        )
        return period is not None and period.status == "confirmed"

    async def _guard_period_open(
        self,
        db: AsyncSession,
        *,
        store_id: UUID,
        date_in_cycle: DateType,
    ) -> None:
        if await self.is_period_confirmed(
            db, store_id=store_id, date_in_cycle=date_in_cycle,
        ):
            raise BadRequestError(
                "This cycle has been confirmed. Entries are locked."
            )

    async def _generate_forms_for_period(
        self, db: AsyncSession, *, period: TipPeriod,
    ) -> int:
        """사이클 확정 직후 직원별 4070 폼 1건씩 생성.

        Box1 cash = sum(cash_tips_kept), Box2 card = own card_tips + received accepted,
        Box3 paid_out = own distributed (accepted/auto_accepted 만), Box4 net = 1+2-3.
        PDF 생성은 별도 단계 — 여기선 row 만 만들고 pdf_key=None 으로.
        """
        # 매장의 entries
        entries = (await db.scalars(
            select(TipEntry).where(
                TipEntry.store_id == period.store_id,
                TipEntry.date >= period.start_date,
                TipEntry.date <= period.end_date,
            )
        )).all()
        if not entries:
            return 0
        entry_ids = [e.id for e in entries]
        dists = (await db.scalars(
            select(TipDistribution).where(TipDistribution.entry_id.in_(entry_ids))
        )).all()

        emp_summary: dict[UUID, dict] = {}
        ent_by_id = {e.id: e for e in entries}
        for e in entries:
            row = emp_summary.setdefault(e.employee_id, {
                "cash": Decimal("0"),
                "own_card": Decimal("0"),
                "paid_out": Decimal("0"),
                "received_card": Decimal("0"),
            })
            row["cash"] += Decimal(str(e.cash_tips_kept))
            row["own_card"] += Decimal(str(e.card_tips))

        for d in dists:
            sender_entry = ent_by_id.get(d.entry_id)
            if sender_entry is None:
                continue
            row = emp_summary.setdefault(sender_entry.employee_id, {
                "cash": Decimal("0"),
                "own_card": Decimal("0"),
                "paid_out": Decimal("0"),
                "received_card": Decimal("0"),
            })
            row["paid_out"] += Decimal(str(d.amount))
            # 받은 사람은 accepted/auto_accepted 만 보고 대상 (가이드 §8.6 Box2).
            if d.status in ("accepted", "auto_accepted") and d.receiver_id is not None:
                rec = emp_summary.setdefault(d.receiver_id, {
                    "cash": Decimal("0"),
                    "own_card": Decimal("0"),
                    "paid_out": Decimal("0"),
                    "received_card": Decimal("0"),
                })
                rec["received_card"] += Decimal(str(d.amount))

        created = 0
        new_forms: list[Form4070Document] = []
        for emp_id, s in emp_summary.items():
            existing = await db.scalar(
                select(Form4070Document).where(
                    Form4070Document.employee_id == emp_id,
                    Form4070Document.period_id == period.id,
                )
            )
            if existing is not None:
                continue
            reported_card = s["own_card"] + s["received_card"]
            net = s["cash"] + reported_card - s["paid_out"]
            form = Form4070Document(
                employee_id=emp_id,
                period_id=period.id,
                reported_cash=s["cash"],
                reported_card=reported_card,
                paid_out=s["paid_out"],
                net_tips=net,
                status="generated",
            )
            db.add(form)
            new_forms.append(form)
            created += 1
        await db.flush()
        # PDF 생성 — 사인 없는 초기 버전 (서명 후 재생성).
        for form in new_forms:
            await self._generate_form_pdf(db, form=form, period=period)
        return created

    async def _generate_form_pdf(
        self,
        db: AsyncSession,
        *,
        form: Form4070Document,
        period: TipPeriod,
        signature_png: Optional[bytes] = None,
        signature_strokes: Optional[dict] = None,
        signed_at_iso: Optional[str] = None,
    ) -> None:
        """PDF 생성 + storage 저장 + form.pdf_key 갱신.

        서명 렌더는 벡터(signature_strokes) 우선, 없으면 레거시 이미지(signature_png).
        """
        from app.services.storage_service import storage_service
        from app.utils.form_4070_pdf import build_form_4070_pdf

        # 직원 + 매장 정보
        emp = await db.scalar(select(User).where(User.id == form.employee_id))
        store = await db.scalar(select(Store).where(Store.id == period.store_id))
        if emp is None or store is None:
            return

        pdf_bytes = build_form_4070_pdf(
            employee_name=emp.full_name or emp.username,
            employee_email=emp.email,
            period_start=period.start_date.isoformat(),
            period_end=period.end_date.isoformat(),
            store_name=store.name,
            cash_tips=f"{Decimal(str(form.reported_cash)):.2f}",
            card_tips=f"{Decimal(str(form.reported_card)):.2f}",
            paid_out=f"{Decimal(str(form.paid_out)):.2f}",
            net_tips=f"{Decimal(str(form.net_tips)):.2f}",
            signed_at=signed_at_iso,
            signature_png=signature_png,
            signature_strokes=signature_strokes,
        )
        key = f"forms/4070/{form.employee_id}/{form.id}.pdf"
        storage_service.save_local(key, pdf_bytes)
        form.pdf_key = key
        await db.flush()

    async def confirm_period(
        self,
        db: AsyncSession,
        *,
        actor: User,
        store_id: UUID,
        date_in_cycle: DateType,
        override_reason: Optional[str] = None,
    ) -> TipPeriod:
        # 확정 전에 pending 분배를 모두 강제 정리 — Box2(received) / Box3(paid_out)
        # 비대칭을 막기 위해 pending_until 도달 여부 무관 모두 auto_accepted 로 전환.
        await self.auto_accept_overdue(db, force=True)
        period = await self.get_or_create_period(
            db, store_id=store_id, date_in_cycle=date_in_cycle,
        )
        if period.status == "confirmed":
            raise BadRequestError("Period is already confirmed")
        period.status = "confirmed"
        period.confirmed_at = datetime.now(timezone.utc)
        period.confirmed_by = actor.id
        if override_reason is not None and override_reason.strip():
            period.override_reason = override_reason.strip()
        self._log(
            db,
            entity_type="tip_period",
            entity_id=period.id,
            action="confirm" if override_reason is None else "force_close",
            actor_id=actor.id,
            after={
                "store_id": str(period.store_id),
                "start_date": period.start_date.isoformat(),
                "end_date": period.end_date.isoformat(),
                "status": period.status,
                "override_reason": period.override_reason,
            },
            comment=override_reason,
        )
        # 사이클 확정 시 직원별 4070 폼 생성 (가이드 §8.6).
        await self._generate_forms_for_period(db, period=period)
        await db.commit()
        return period

    # ── Forms & signature ────────────────────────────────────

    async def list_forms_for_employee(
        self, db: AsyncSession, *, employee_id: UUID,
    ) -> list[Form4070Document]:
        return list((await db.scalars(
            select(Form4070Document)
            .where(Form4070Document.employee_id == employee_id)
            .order_by(Form4070Document.generated_at.desc())
        )).all())

    async def list_forms_for_period(
        self, db: AsyncSession, *, period_id: UUID,
    ) -> list[Form4070Document]:
        return list((await db.scalars(
            select(Form4070Document)
            .where(Form4070Document.period_id == period_id)
        )).all())

    async def sign_form(
        self,
        db: AsyncSession,
        *,
        actor: User,
        form_id: UUID,
        signature_strokes: Optional[dict] = None,
        signature_image_key: Optional[str] = None,
        save_for_future: bool = False,
    ) -> Form4070Document:
        """4070 폼에 서명 적용 — 벡터 strokes 우선, 레거시 image_key fallback.

        signature_strokes 가 주어지면 그 벡터를 form 에 박제(스냅샷)하고 PDF 를
        벡터로 렌더한다. 없으면 signature_image_key(레거시) 경로로 이미지 렌더.
        save_for_future=True 면 users.signature_strokes(벡터 우선) 또는
        signature_image_key(레거시) 를 갱신해 다음 폼에서 재사용.
        """
        if not signature_strokes and not signature_image_key:
            raise BadRequestError("Either strokes or signature_image_key is required")
        form = await db.scalar(
            select(Form4070Document).where(Form4070Document.id == form_id)
        )
        if form is None:
            raise NotFoundError(f"Form {form_id} not found")
        if form.employee_id != actor.id:
            raise ForbiddenError("Cannot sign another employee's form")
        if form.status == "signed":
            raise BadRequestError("Form is already signed")

        if signature_strokes:
            # 벡터 스냅샷 박제 — 유저 저장 서명이 나중에 바뀌어도 불변.
            form.signature_strokes = signature_strokes
            form.signature_image_key = None
        else:
            # 레거시 경로 — strokes 없이 이미지 키만.
            form.signature_image_key = signature_image_key
        form.signed_at = datetime.now(timezone.utc)
        form.status = "signed"

        if save_for_future:
            user = await db.scalar(select(User).where(User.id == actor.id))
            if user is not None:
                if signature_strokes:
                    user.signature_strokes = signature_strokes
                elif signature_image_key:
                    user.signature_image_key = signature_image_key
        self._log(
            db,
            entity_type="form_4070",
            entity_id=form.id,
            action="sign",
            actor_id=actor.id,
            after={"status": "signed", "method": "vector" if signature_strokes else "image"},
        )
        # PDF 재생성 — 서명 overlay 적용 (벡터 우선, 없으면 레거시 이미지).
        period = await db.scalar(select(TipPeriod).where(TipPeriod.id == form.period_id))
        sig_png = (
            None if signature_strokes
            else self._read_signature_bytes(signature_image_key)
        )
        if period is not None:
            await self._generate_form_pdf(
                db,
                form=form,
                period=period,
                signature_png=sig_png,
                signature_strokes=signature_strokes,
                signed_at_iso=form.signed_at.isoformat(),
            )
        await db.commit()
        return form

    @staticmethod
    def _read_signature_bytes(key: Optional[str]) -> Optional[bytes]:
        """로컬 bucket 에 있는 사인 이미지를 raw bytes 로 읽음. S3 모드면 None."""
        if not key:
            return None
        try:
            from pathlib import Path
            from app.services.storage_service import BUCKET_DIR
            p = Path(BUCKET_DIR) / key
            if p.exists():
                return p.read_bytes()
        except Exception:
            pass
        return None

    async def regenerate_pdf(
        self, db: AsyncSession, *, form: Form4070Document, pdf_key: str,
    ) -> None:
        form.pdf_key = pdf_key
        await db.flush()

    # ── 헬퍼 ──────────────────────────────────────────────────

    @staticmethod
    def _distribution_total(distributions: Iterable[TipDistribution | TipDistributionCreate]) -> Decimal:
        return sum((Decimal(str(d.amount)) for d in distributions), Decimal("0"))

    @staticmethod
    def _validate_distribution_total(card_tips: Decimal, dist_total: Decimal) -> None:
        if dist_total > card_tips:
            raise BadRequestError(
                f"Distributed exceeds card tips by ${(dist_total - card_tips):.2f}"
            )

    @staticmethod
    def _validate_no_duplicate_receivers(
        distributions: Iterable[TipDistributionCreate],
    ) -> None:
        """같은 receiver 에 2번 분배 금지 — 한 줄로 합쳐서 입력하게 강제."""
        seen: set[UUID] = set()
        for d in distributions:
            if d.receiver_id in seen:
                raise BadRequestError(
                    "Same coworker selected more than once in distributions. "
                    "Combine into a single row."
                )
            seen.add(d.receiver_id)

    async def get_eligible_receivers(
        self,
        db: AsyncSession,
        *,
        schedule_id: UUID,
        asking_user_id: UUID,
        organization_id: UUID,
    ) -> list[dict]:
        """주어진 schedule 의 동료 후보 — 실제 clock-in 한 동료만.

        본인 제외. 신규 정책 (L4): peer 는 반드시 attendance.clock_in 이 있어야 함
        (= 실제 출근한 동료). schedule 만 있고 clock-in 안 한 사람은 제외.
        schedule 없이 임시 일한 동료는 client 의 manual add 흐름으로 별도 추가.

        기존 시간 window overlap 검사는 그대로 유지: 본인 clock_in/clock_out
        구간과 겹친 peer 만.

        Returns: [{"id": str, "full_name": str}] — 정렬: 이름 오름차순.
        """
        from app.models.attendance import Attendance
        from app.models.user import User
        from datetime import datetime, time, timezone as tz_module
        from zoneinfo import ZoneInfo
        from app.utils.timezone import get_store_day_config

        # 1) 본인 schedule 로 store/date 확정
        sched = await db.scalar(
            select(Schedule).where(
                Schedule.id == schedule_id,
                Schedule.organization_id == organization_id,
            )
        )
        if sched is None:
            raise BadRequestError("Schedule not found")
        if sched.user_id != asking_user_id:
            raise BadRequestError("Schedule does not belong to you")

        # 2) 같은 매장 + 같은 날 + confirmed schedule 의 다른 user_id 들
        peer_rows = await db.execute(
            select(Schedule).where(
                Schedule.store_id == sched.store_id,
                Schedule.work_date == sched.work_date,
                Schedule.status == "confirmed",
                Schedule.user_id != asking_user_id,
            )
        )
        peer_schedules: list[Schedule] = list(peer_rows.scalars().all())
        if not peer_schedules:
            return []

        # 3) 본인 attendance 가져오기 — clock_in/clock_out 또는 schedule 시간으로 window 결정.
        my_att = await db.scalar(
            select(Attendance).where(Attendance.schedule_id == sched.id)
        )

        store_tz, _ = await get_store_day_config(db, sched.store_id)
        store_zone = ZoneInfo(store_tz)

        def _to_dt(d, t: time | None) -> datetime | None:
            """schedule date+time 을 store tz aware datetime 으로. clock_in 과 비교 가능하게 UTC 로 정규화."""
            if t is None:
                return None
            local = datetime.combine(d, t).replace(tzinfo=store_zone)
            return local.astimezone(tz_module.utc)

        my_start: datetime | None = my_att.clock_in if my_att and my_att.clock_in else _to_dt(sched.work_date, sched.start_time)
        my_end: datetime | None = my_att.clock_out if my_att and my_att.clock_out else _to_dt(sched.work_date, sched.end_time)

        # window 계산 못하면 시간 필터 skip (같은 날 같은 매장 모두 반환)
        skip_overlap = my_start is None or my_end is None

        # 4) peer attendance 일괄 조회 — 시간 비교에 사용
        peer_schedule_ids = [s.id for s in peer_schedules]
        peer_atts_rows = await db.execute(
            select(Attendance).where(Attendance.schedule_id.in_(peer_schedule_ids))
        )
        peer_atts: dict[UUID, Attendance] = {
            a.schedule_id: a for a in peer_atts_rows.scalars().all()
        }

        # 5) 시간 overlap 검사 + 결과 user_id 집합
        #    L4 정책: peer 는 실제 clock-in 한 사람만 (attendance + clock_in IS NOT NULL).
        eligible_user_ids: list[UUID] = []
        for s in peer_schedules:
            att = peer_atts.get(s.id)
            if att is None or att.clock_in is None:
                continue  # 출근 안 한 동료는 자동 후보에서 제외
            if skip_overlap:
                eligible_user_ids.append(s.user_id)
                continue
            other_start = att.clock_in
            # peer 가 아직 clock-out 안 했으면 schedule end 또는 현재 시각 추정 대신
            # 안전하게 포함 (본인은 이미 clock-out 한 시점이므로 overlap 가능성 큼).
            other_end = att.clock_out or _to_dt(s.work_date, s.end_time)
            if other_end is None:
                eligible_user_ids.append(s.user_id)
                continue
            # 좌-닫힘 개구간 교집합
            if my_start < other_end and other_start < my_end:
                eligible_user_ids.append(s.user_id)

        if not eligible_user_ids:
            return []

        # 6) user 객체 로드 + name 정렬
        users_rows = await db.execute(
            select(User)
            .where(
                User.id.in_(eligible_user_ids),
                User.organization_id == organization_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
            .order_by(User.full_name.asc())
        )
        return [
            {"id": str(u.id), "full_name": u.full_name or u.username}
            for u in users_rows.scalars().all()
        ]

    async def _resolve_work_role_snapshot(
        self,
        db: AsyncSession,
        work_role_id: Optional[UUID],
    ) -> Optional[str]:
        """work_role 의 표시명을 snapshot. shift/position 의 조합도 시도."""
        if work_role_id is None:
            return None
        from sqlalchemy.orm import selectinload
        from app.models.work import Shift, Position
        row = await db.scalar(
            select(StoreWorkRole).where(StoreWorkRole.id == work_role_id)
        )
        if row is None:
            return None
        if row.name:
            return row.name
        # name 이 비어있으면 shift/position 조합으로 fallback
        shift = await db.scalar(select(Shift.name).where(Shift.id == row.shift_id))
        position = await db.scalar(select(Position.name).where(Position.id == row.position_id))
        parts = [p for p in (shift, position) if p]
        return " · ".join(parts) if parts else None

    async def _resolve_receiver_name(self, db: AsyncSession, receiver_id: UUID) -> str:
        row = await db.scalar(select(User).where(User.id == receiver_id))
        if row is None:
            raise BadRequestError(f"Recipient {receiver_id} not found")
        return row.full_name or row.email or ""

    @staticmethod
    def _log(
        db: AsyncSession,
        *,
        entity_type: str,
        entity_id: UUID,
        action: str,
        actor_id: Optional[UUID],
        before: Optional[dict] = None,
        after: Optional[dict] = None,
        comment: Optional[str] = None,
    ) -> None:
        db.add(TipAuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor_id=actor_id,
            comment=comment,
            before=before,
            after=after,
        ))

    @staticmethod
    def _entry_snapshot(entry: TipEntry) -> dict:
        return {
            "store_id": str(entry.store_id),
            "employee_id": str(entry.employee_id),
            "work_role_id": str(entry.work_role_id) if entry.work_role_id else None,
            "date": entry.date.isoformat(),
            "card_tips": str(entry.card_tips),
            "cash_tips_kept": str(entry.cash_tips_kept),
            "source": entry.source,
        }

    @staticmethod
    def _dist_snapshot(d: TipDistribution) -> dict:
        return {
            "id": str(d.id),
            "receiver_id": str(d.receiver_id) if d.receiver_id else None,
            "receiver_name": d.receiver_name_snapshot,
            "amount": str(d.amount),
            "reason": d.reason,
            "status": d.status,
        }

    # ── Create entry ─────────────────────────────────────────

    async def _load_user_schedule(
        self, db: AsyncSession, *, user_id: UUID, schedule_id: UUID,
    ) -> Schedule:
        """user 본인의 schedule 만 허용. 못 찾으면 400."""
        sched = await db.scalar(
            select(Schedule).where(
                Schedule.id == schedule_id, Schedule.user_id == user_id
            )
        )
        if sched is None:
            raise BadRequestError("Schedule not found or not yours")
        return sched

    async def _existing_entry_for_schedule(
        self, db: AsyncSession, *, employee_id: UUID, schedule_id: UUID,
    ) -> Optional[TipEntry]:
        return await db.scalar(
            select(TipEntry).where(
                TipEntry.employee_id == employee_id,
                TipEntry.schedule_id == schedule_id,
            )
        )

    async def create_entry(
        self,
        db: AsyncSession,
        *,
        actor: User,
        payload: TipEntryCreate,
    ) -> TipEntry:
        # 1) 분배 합 + 중복 receiver 검증
        dist_total = self._distribution_total(payload.distributions)
        self._validate_distribution_total(payload.card_tips, dist_total)
        self._validate_no_duplicate_receivers(payload.distributions)

        # 2) schedule 로부터 store/work_role/date 자동 derive
        sched = await self._load_user_schedule(
            db, user_id=actor.id, schedule_id=payload.schedule_id,
        )
        # 사이클이 confirmed 면 잠금
        await self._guard_period_open(
            db, store_id=sched.store_id, date_in_cycle=sched.work_date,
        )
        # idempotent: 같은 schedule 에 이미 entry 있으면 400 (수정은 PATCH 로).
        if await self._existing_entry_for_schedule(
            db, employee_id=actor.id, schedule_id=payload.schedule_id,
        ):
            raise BadRequestError(
                "Tip entry already exists for this schedule. Edit it instead."
            )
        work_role_name = await self._resolve_work_role_snapshot(db, sched.work_role_id)

        now = datetime.now(timezone.utc)
        entry = TipEntry(
            schedule_id=sched.id,
            store_id=sched.store_id,
            employee_id=actor.id,
            work_role_id=sched.work_role_id,
            work_role_name_snapshot=work_role_name,
            date=sched.work_date,
            card_tips=payload.card_tips,
            cash_tips_kept=payload.cash_tips_kept,
            source=payload.source,
            last_modified_by_id=actor.id,
            last_modified_at=now,
        )
        db.add(entry)
        await db.flush()

        # 3) distributions 생성
        pending_until = now + timedelta(hours=AUTO_ACCEPT_HOURS)
        for d in payload.distributions:
            receiver_name = await self._resolve_receiver_name(db, d.receiver_id)
            dist = TipDistribution(
                entry_id=entry.id,
                receiver_id=d.receiver_id,
                receiver_name_snapshot=receiver_name,
                amount=d.amount,
                reason=d.reason,
                status="pending",
                pending_until=pending_until,
            )
            db.add(dist)
            await db.flush()
            self._log(
                db,
                entity_type="tip_distribution",
                entity_id=dist.id,
                action="create",
                actor_id=actor.id,
                after=self._dist_snapshot(dist),
            )

        # 4) audit log — entry create
        self._log(
            db,
            entity_type="tip_entry",
            entity_id=entry.id,
            action="create",
            actor_id=actor.id,
            after=self._entry_snapshot(entry),
        )
        await db.commit()
        await db.refresh(entry)
        return entry

    # ── Update entry ─────────────────────────────────────────

    async def update_entry(
        self,
        db: AsyncSession,
        *,
        actor: User,
        entry_id: UUID,
        payload: TipEntryUpdate,
    ) -> TipEntry:
        entry = await self._get_entry_with_dists(db, entry_id)
        if entry.employee_id != actor.id:
            raise ForbiddenError("Cannot edit another staff's entry from app API")
        await self._guard_period_open(
            db, store_id=entry.store_id, date_in_cycle=entry.date,
        )

        before_snap = self._entry_snapshot(entry)

        new_card = payload.card_tips if payload.card_tips is not None else entry.card_tips
        new_cash = payload.cash_tips_kept if payload.cash_tips_kept is not None else entry.cash_tips_kept

        # 분배 변경 결정 + 중복 receiver 검증
        if payload.distributions is not None:
            self._validate_no_duplicate_receivers(payload.distributions)
            new_dist_total = self._distribution_total(payload.distributions)
        else:
            new_dist_total = self._distribution_total(entry.distributions)
        self._validate_distribution_total(new_card, new_dist_total)

        entry.card_tips = new_card
        entry.cash_tips_kept = new_cash
        entry.last_modified_by_id = actor.id
        entry.last_modified_at = datetime.now(timezone.utc)

        # 분배 교체
        if payload.distributions is not None:
            for d in entry.distributions:
                self._log(
                    db,
                    entity_type="tip_distribution",
                    entity_id=d.id,
                    action="delete",
                    actor_id=actor.id,
                    before=self._dist_snapshot(d),
                )
                await db.delete(d)
            await db.flush()

            pending_until = datetime.now(timezone.utc) + timedelta(hours=AUTO_ACCEPT_HOURS)
            for d in payload.distributions:
                receiver_name = await self._resolve_receiver_name(db, d.receiver_id)
                dist = TipDistribution(
                    entry_id=entry.id,
                    receiver_id=d.receiver_id,
                    receiver_name_snapshot=receiver_name,
                    amount=d.amount,
                    reason=d.reason,
                    status="pending",
                    pending_until=pending_until,
                )
                db.add(dist)
                await db.flush()
                self._log(
                    db,
                    entity_type="tip_distribution",
                    entity_id=dist.id,
                    action="create",
                    actor_id=actor.id,
                    after=self._dist_snapshot(dist),
                )

        self._log(
            db,
            entity_type="tip_entry",
            entity_id=entry.id,
            action="update",
            actor_id=actor.id,
            before=before_snap,
            after=self._entry_snapshot(entry),
        )
        await db.commit()
        # 다시 로딩 (distributions 새로고침)
        return await self._get_entry_with_dists(db, entry.id)

    # ── List ──────────────────────────────────────────────────

    async def latest_manager_note(
        self, db: AsyncSession, *, entry_id: UUID, employee_id: UUID,
    ) -> tuple[Optional[str], Optional[str]]:
        """entry 의 가장 최근 매니저 수정 audit log 의 (comment, actor_name).

        매니저(actor_id != employee_id) 가 마지막으로 update 한 로그를 찾는다.
        가이드 §2.2.2 — 직원이 매니저 수정 사유를 확인할 수 있게.
        """
        log = await db.scalar(
            select(TipAuditLog)
            .where(
                TipAuditLog.entity_type == "tip_entry",
                TipAuditLog.entity_id == entry_id,
                TipAuditLog.action.in_(("update", "create")),
                TipAuditLog.actor_id.is_not(None),
                TipAuditLog.actor_id != employee_id,
                TipAuditLog.comment.is_not(None),
            )
            .order_by(TipAuditLog.created_at.desc())
        )
        if log is None:
            return None, None
        name = None
        if log.actor_id is not None:
            name = await db.scalar(
                select(User.full_name).where(User.id == log.actor_id)
            )
        return log.comment, name

    async def _get_entry_with_dists(self, db: AsyncSession, entry_id: UUID) -> TipEntry:
        """Entry + distributions + schedule 같이 로드 (transient attrs)."""
        entry = await db.scalar(select(TipEntry).where(TipEntry.id == entry_id))
        if entry is None:
            raise NotFoundError(f"Tip entry {entry_id} not found")
        dists = (await db.scalars(
            select(TipDistribution).where(TipDistribution.entry_id == entry_id)
        )).all()
        setattr(entry, "distributions", list(dists))
        sched = None
        if entry.schedule_id is not None:
            sched = await db.scalar(
                select(Schedule).where(Schedule.id == entry.schedule_id)
            )
        setattr(entry, "_schedule_loaded", sched)
        return entry

    async def list_my_entries(
        self,
        db: AsyncSession,
        *,
        employee_id: UUID,
        start: DateType,
        end: DateType,
        store_id: Optional[UUID] = None,
    ) -> list[TipEntry]:
        stmt = (
            select(TipEntry)
            .where(
                TipEntry.employee_id == employee_id,
                TipEntry.date >= start,
                TipEntry.date <= end,
            )
            .order_by(TipEntry.date.asc(), TipEntry.created_at.asc())
        )
        if store_id is not None:
            stmt = stmt.where(TipEntry.store_id == store_id)
        entries = (await db.scalars(stmt)).all()
        if not entries:
            return []
        # 분배 batch 로드
        entry_ids = [e.id for e in entries]
        dists = (await db.scalars(
            select(TipDistribution).where(TipDistribution.entry_id.in_(entry_ids))
        )).all()
        by_entry: dict[UUID, list[TipDistribution]] = {eid: [] for eid in entry_ids}
        for d in dists:
            by_entry[d.entry_id].append(d)
        # schedule batch 로드 — 시간 표시용
        sched_ids = {e.schedule_id for e in entries if e.schedule_id}
        scheds: dict[UUID, Schedule] = {}
        if sched_ids:
            for s in (await db.scalars(
                select(Schedule).where(Schedule.id.in_(sched_ids))
            )).all():
                scheds[s.id] = s
        for e in entries:
            setattr(e, "distributions", by_entry.get(e.id, []))
            setattr(e, "_schedule_loaded", scheds.get(e.schedule_id) if e.schedule_id else None)
        return list(entries)

    # ── Incoming distributions ────────────────────────────────

    async def auto_accept_overdue(
        self, db: AsyncSession, *, force: bool = False,
    ) -> int:
        """pending 분배를 auto_accepted 로 일괄 전환.

        force=False (기본): pending_until 지난 것만 — cron 또는 list 조회 시 호출.
        force=True: 모든 pending 강제 처리 — 사이클 확정 시점에 box2/box3 비대칭을 막기 위함.
        idempotent. 반환: 새로 처리된 분배 수.
        """
        now = datetime.now(timezone.utc)
        stmt = select(TipDistribution).where(TipDistribution.status == "pending")
        if not force:
            stmt = stmt.where(TipDistribution.pending_until <= now)
        rows = (await db.scalars(stmt)).all()
        for d in rows:
            before = self._dist_snapshot(d)
            d.status = "auto_accepted"
            d.accepted_at = now
            self._log(
                db,
                entity_type="tip_distribution",
                entity_id=d.id,
                action="auto_accept",
                actor_id=None,
                before=before,
                after=self._dist_snapshot(d),
                comment="Force-accepted on cycle confirm" if force else None,
            )
        if rows:
            await db.commit()
        return len(rows)

    async def list_incoming(
        self,
        db: AsyncSession,
        *,
        receiver_id: UUID,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        # 조회 시 lazy auto-accept 적용 — 별도 cron 없이도 빠짐 없이 처리.
        await self.auto_accept_overdue(db)
        stmt = (
            select(TipDistribution, TipEntry, User, Store, StoreWorkRole)
            .join(TipEntry, TipEntry.id == TipDistribution.entry_id)
            .join(User, User.id == TipEntry.employee_id)
            .join(Store, Store.id == TipEntry.store_id)
            .join(StoreWorkRole, StoreWorkRole.id == TipEntry.work_role_id, isouter=True)
            .where(TipDistribution.receiver_id == receiver_id)
            .order_by(TipDistribution.created_at.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(TipDistribution.status == status)
        rows = (await db.execute(stmt)).all()

        out: list[dict] = []
        for dist, entry, sender, store, work_role in rows:
            out.append({
                "id": dist.id,
                "entry_id": entry.id,
                "sender_id": sender.id,
                "sender_name": sender.full_name or sender.email or "",
                "sender_store_id": store.id,
                "sender_store_name": store.name,
                "work_role_name": (entry.work_role_name_snapshot or (work_role.name if work_role else None)),
                "work_date": entry.date,
                "amount": dist.amount,
                "reason": dist.reason,
                "status": dist.status,
                "pending_until": dist.pending_until,
                "accepted_at": dist.accepted_at,
                "created_at": dist.created_at,
            })
        return out

    # ── Accept distribution ───────────────────────────────────

    async def accept_distribution(
        self,
        db: AsyncSession,
        *,
        actor: User,
        distribution_id: UUID,
    ) -> TipDistribution:
        dist = await db.scalar(
            select(TipDistribution).where(TipDistribution.id == distribution_id)
        )
        if dist is None:
            raise NotFoundError(f"Distribution {distribution_id} not found")
        if dist.receiver_id != actor.id:
            raise ForbiddenError("Cannot accept distribution sent to another staff")
        if dist.status != "pending":
            # 이미 처리됨 — idempotent 처리
            return dist

        before = self._dist_snapshot(dist)
        dist.status = "accepted"
        dist.accepted_at = datetime.now(timezone.utc)
        self._log(
            db,
            entity_type="tip_distribution",
            entity_id=dist.id,
            action="accept",
            actor_id=actor.id,
            before=before,
            after=self._dist_snapshot(dist),
        )
        await db.commit()
        await db.refresh(dist)
        return dist

    # ── Response builders ─────────────────────────────────────

    # ── Manager (console) — Stage A 매니저 흐름 ────────────────

    async def list_entries_for_store(
        self,
        db: AsyncSession,
        *,
        store_id: UUID,
        start: DateType,
        end: DateType,
        employee_id: Optional[UUID] = None,
    ) -> list[TipEntry]:  # noqa: not strictly only used by manager

        stmt = (
            select(TipEntry)
            .where(
                TipEntry.store_id == store_id,
                TipEntry.date >= start,
                TipEntry.date <= end,
            )
            .order_by(TipEntry.date.asc(), TipEntry.created_at.asc())
        )
        if employee_id is not None:
            stmt = stmt.where(TipEntry.employee_id == employee_id)
        entries = (await db.scalars(stmt)).all()
        if not entries:
            return []
        entry_ids = [e.id for e in entries]
        dists = (await db.scalars(
            select(TipDistribution).where(TipDistribution.entry_id.in_(entry_ids))
        )).all()
        by_entry: dict[UUID, list[TipDistribution]] = {eid: [] for eid in entry_ids}
        for d in dists:
            by_entry[d.entry_id].append(d)
        # schedule batch — 시간 표시용
        sched_ids = {e.schedule_id for e in entries if e.schedule_id}
        scheds: dict[UUID, Schedule] = {}
        if sched_ids:
            for s in (await db.scalars(
                select(Schedule).where(Schedule.id.in_(sched_ids))
            )).all():
                scheds[s.id] = s
        for e in entries:
            setattr(e, "distributions", by_entry.get(e.id, []))
            setattr(e, "_schedule_loaded", scheds.get(e.schedule_id) if e.schedule_id else None)
        return list(entries)

    async def manager_create_entry(
        self,
        db: AsyncSession,
        *,
        actor: User,
        employee_id: UUID,
        schedule_id: Optional[UUID],
        store_id: Optional[UUID],
        work_role_id: Optional[UUID],
        work_date: Optional[DateType],
        card_tips: Decimal,
        cash_tips_kept: Decimal,
        comment: str,
        distributions: list[TipDistributionCreate],
    ) -> TipEntry:
        """매니저가 직원 대신 entry 추가 — comment 필수.

        schedule_id 가 있으면 schedule 에서 store/work_role/date derive.
        없으면 freeform — store_id + work_date 가 직접 들어와야 한다.
        """
        if not comment or not comment.strip():
            raise BadRequestError("Manager comment is required")
        dist_total = self._distribution_total(distributions)
        self._validate_distribution_total(card_tips, dist_total)

        # 사이클 잠금 사전 검사 — 어떤 store/date 든 매니저가 신규 entry 를 confirmed
        # 사이클에 끼워넣지 못하게.
        guard_store = store_id
        guard_date = work_date

        if schedule_id is not None:
            sched = await db.scalar(
                select(Schedule).where(
                    Schedule.id == schedule_id,
                    Schedule.user_id == employee_id,
                )
            )
            if sched is None:
                raise BadRequestError("Schedule not found or not for this employee")
            if await self._existing_entry_for_schedule(
                db, employee_id=employee_id, schedule_id=schedule_id,
            ):
                raise BadRequestError(
                    "Tip entry already exists for this schedule. Edit it instead."
                )
            resolved_store_id = sched.store_id
            resolved_work_role_id = sched.work_role_id
            resolved_date = sched.work_date
            guard_store = sched.store_id
            guard_date = sched.work_date
        else:
            if store_id is None or work_date is None:
                raise BadRequestError("store_id and date are required when schedule_id is omitted")
            resolved_store_id = store_id
            resolved_work_role_id = work_role_id
            resolved_date = work_date

        await self._guard_period_open(
            db, store_id=guard_store, date_in_cycle=guard_date,
        )

        work_role_name = await self._resolve_work_role_snapshot(db, resolved_work_role_id)
        now = datetime.now(timezone.utc)
        entry = TipEntry(
            schedule_id=schedule_id,
            store_id=resolved_store_id,
            employee_id=employee_id,
            work_role_id=resolved_work_role_id,
            work_role_name_snapshot=work_role_name,
            date=resolved_date,
            card_tips=card_tips,
            cash_tips_kept=cash_tips_kept,
            source="manager",
            last_modified_by_id=actor.id,
            last_modified_at=now,
        )
        db.add(entry)
        await db.flush()
        pending_until = now + timedelta(hours=AUTO_ACCEPT_HOURS)
        for d in distributions:
            receiver_name = await self._resolve_receiver_name(db, d.receiver_id)
            dist = TipDistribution(
                entry_id=entry.id,
                receiver_id=d.receiver_id,
                receiver_name_snapshot=receiver_name,
                amount=d.amount,
                reason=d.reason,
                status="pending",
                pending_until=pending_until,
            )
            db.add(dist)
            await db.flush()
            self._log(
                db, entity_type="tip_distribution", entity_id=dist.id,
                action="create", actor_id=actor.id,
                after=self._dist_snapshot(dist), comment=comment,
            )
        self._log(
            db, entity_type="tip_entry", entity_id=entry.id,
            action="create", actor_id=actor.id,
            after=self._entry_snapshot(entry), comment=comment,
        )
        await db.commit()
        return await self._get_entry_with_dists(db, entry.id)

    async def manager_update_entry(
        self,
        db: AsyncSession,
        *,
        actor: User,
        entry_id: UUID,
        comment: str,
        card_tips: Optional[Decimal],
        cash_tips_kept: Optional[Decimal],
        distributions: Optional[list[TipDistributionCreate]],
    ) -> TipEntry:
        if not comment or not comment.strip():
            raise BadRequestError("Manager comment is required")
        entry = await self._get_entry_with_dists(db, entry_id)
        await self._guard_period_open(
            db, store_id=entry.store_id, date_in_cycle=entry.date,
        )
        before_snap = self._entry_snapshot(entry)

        new_card = card_tips if card_tips is not None else entry.card_tips
        new_cash = cash_tips_kept if cash_tips_kept is not None else entry.cash_tips_kept
        new_dist_total = (
            self._distribution_total(distributions)
            if distributions is not None
            else self._distribution_total(entry.distributions)
        )
        self._validate_distribution_total(new_card, new_dist_total)

        entry.card_tips = new_card
        entry.cash_tips_kept = new_cash
        entry.last_modified_by_id = actor.id
        entry.last_modified_at = datetime.now(timezone.utc)

        if distributions is not None:
            for d in entry.distributions:
                self._log(
                    db, entity_type="tip_distribution", entity_id=d.id,
                    action="delete", actor_id=actor.id,
                    before=self._dist_snapshot(d), comment=comment,
                )
                await db.delete(d)
            await db.flush()
            pending_until = datetime.now(timezone.utc) + timedelta(hours=AUTO_ACCEPT_HOURS)
            for d in distributions:
                receiver_name = await self._resolve_receiver_name(db, d.receiver_id)
                dist = TipDistribution(
                    entry_id=entry.id,
                    receiver_id=d.receiver_id,
                    receiver_name_snapshot=receiver_name,
                    amount=d.amount,
                    reason=d.reason,
                    status="pending",
                    pending_until=pending_until,
                )
                db.add(dist)
                await db.flush()
                self._log(
                    db, entity_type="tip_distribution", entity_id=dist.id,
                    action="create", actor_id=actor.id,
                    after=self._dist_snapshot(dist), comment=comment,
                )

        self._log(
            db, entity_type="tip_entry", entity_id=entry.id,
            action="update", actor_id=actor.id,
            before=before_snap, after=self._entry_snapshot(entry), comment=comment,
        )
        # 직원에게 alert — 매니저가 본인 entry 수정 시 즉시 알림.
        if entry.employee_id != actor.id:
            await self._notify_employee_of_manager_change(
                db, entry=entry, comment=comment,
            )
        await db.commit()
        return await self._get_entry_with_dists(db, entry.id)

    async def _notify_employee_of_manager_change(
        self,
        db: AsyncSession,
        *,
        entry: TipEntry,
        comment: str,
    ) -> None:
        """매니저가 직원 entry 수정 시 alert 생성. organization_id 는 직원에서 조회."""
        emp = await db.scalar(
            select(User.organization_id).where(User.id == entry.employee_id)
        )
        if emp is None:
            return
        db.add(Alert(
            organization_id=emp,
            user_id=entry.employee_id,
            type="tip_manager_change",
            message=f"Manager updated your tip entry for {entry.date.isoformat()}: {comment[:200]}",
            reference_type="tip_entry",
            reference_id=entry.id,
        ))

    async def list_store_distributions(
        self,
        db: AsyncSession,
        *,
        store_id: UUID,
        status_filter: Optional[str] = None,
        start: Optional[DateType] = None,
        end: Optional[DateType] = None,
        limit: int = 200,
    ) -> list[dict]:
        """매장 단위 분배 목록 — sender(entry.employee) 가 해당 매장인 것 기준.

        start/end (entry.date 범위) 가 주어지면 그 사이클 분배만 반환.
        """
        await self.auto_accept_overdue(db)
        stmt = (
            select(TipDistribution, TipEntry, User, StoreWorkRole)
            .join(TipEntry, TipEntry.id == TipDistribution.entry_id)
            .join(User, User.id == TipEntry.employee_id)
            .join(StoreWorkRole, StoreWorkRole.id == TipEntry.work_role_id, isouter=True)
            .where(TipEntry.store_id == store_id)
            .order_by(TipDistribution.created_at.desc())
            .limit(limit)
        )
        if status_filter:
            stmt = stmt.where(TipDistribution.status == status_filter)
        if start is not None:
            stmt = stmt.where(TipEntry.date >= start)
        if end is not None:
            stmt = stmt.where(TipEntry.date <= end)
        rows = (await db.execute(stmt)).all()
        out: list[dict] = []
        for dist, entry, sender, work_role in rows:
            out.append({
                "id": dist.id,
                "entry_id": entry.id,
                "sender_id": sender.id,
                "sender_name": sender.full_name or sender.email or "",
                "receiver_id": dist.receiver_id,
                "receiver_name": dist.receiver_name_snapshot,
                "work_role_name": (entry.work_role_name_snapshot or (work_role.name if work_role else None)),
                "work_date": entry.date,
                "amount": dist.amount,
                "reason": dist.reason,
                "status": dist.status,
                "pending_until": dist.pending_until,
                "accepted_at": dist.accepted_at,
                "created_at": dist.created_at,
            })
        return out

    # ── Period dashboard 집계 ────────────────────────────────

    async def get_period_dashboard(
        self,
        db: AsyncSession,
        *,
        store_id: UUID,
        date_in_cycle: DateType,
    ) -> dict:
        """Period 탭 데이터: KPI 5개 + daily totals + per-employee 합계.

        period 가 아직 없으면 자동 생성하지 않고 status='open' 으로 응답한다.
        """
        start, end = cycle_for_date(date_in_cycle)
        period = await db.scalar(
            select(TipPeriod).where(
                TipPeriod.store_id == store_id,
                TipPeriod.start_date == start,
                TipPeriod.end_date == end,
            )
        )

        # entries
        entries = (await db.scalars(
            select(TipEntry).where(
                TipEntry.store_id == store_id,
                TipEntry.date >= start,
                TipEntry.date <= end,
            )
        )).all()
        entry_ids = [e.id for e in entries]
        dists = (await db.scalars(
            select(TipDistribution).where(TipDistribution.entry_id.in_(entry_ids))
        )).all() if entry_ids else []

        # KPI
        kpi = {
            "card_total": Decimal("0"),
            "cash_total": Decimal("0"),
            "distributed_total": Decimal("0"),
            "reported_total": Decimal("0"),
            "entries_count": len(entries),
            "distinct_employees": len({e.employee_id for e in entries}),
        }
        # daily totals
        daily: dict[DateType, Decimal] = {}
        # per-employee
        per_employee: dict[UUID, dict] = {}
        # entry → distributions 매핑
        dists_by_entry: dict[UUID, list[TipDistribution]] = {}
        for d in dists:
            dists_by_entry.setdefault(d.entry_id, []).append(d)

        for e in entries:
            ent_dists = dists_by_entry.get(e.id, [])
            dist_total = sum((Decimal(str(d.amount)) for d in ent_dists), Decimal("0"))
            reportable_card = Decimal(str(e.card_tips)) - dist_total
            reported = Decimal(str(e.cash_tips_kept)) + reportable_card

            kpi["card_total"] += Decimal(str(e.card_tips))
            kpi["cash_total"] += Decimal(str(e.cash_tips_kept))
            kpi["distributed_total"] += dist_total
            kpi["reported_total"] += reported

            daily[e.date] = daily.get(e.date, Decimal("0")) + reported

            row = per_employee.setdefault(e.employee_id, {
                "employee_id": e.employee_id,
                "card": Decimal("0"),
                "cash": Decimal("0"),
                "distributed": Decimal("0"),
                "reported": Decimal("0"),
                "entries": 0,
            })
            row["card"] += Decimal(str(e.card_tips))
            row["cash"] += Decimal(str(e.cash_tips_kept))
            row["distributed"] += dist_total
            row["reported"] += reported
            row["entries"] += 1

        # employee 이름
        emp_ids = list(per_employee.keys())
        names: dict[UUID, str] = {}
        if emp_ids:
            rows = (await db.execute(
                select(User.id, User.full_name).where(User.id.in_(emp_ids))
            )).all()
            for uid, name in rows:
                names[uid] = name or ""

        per_employee_list = sorted(
            (
                {**row, "employee_name": names.get(row["employee_id"], "")}
                for row in per_employee.values()
            ),
            key=lambda r: r["reported"],
            reverse=True,
        )

        # daily list (사이클 전체 일자 채우기)
        daily_list: list[dict] = []
        cursor = start
        while cursor <= end:
            daily_list.append({
                "date": cursor,
                "reported": daily.get(cursor, Decimal("0")),
            })
            cursor = DateType.fromordinal(cursor.toordinal() + 1)

        return {
            "store_id": store_id,
            "start_date": start,
            "end_date": end,
            "status": period.status if period else "open",
            "confirmed_at": period.confirmed_at if period else None,
            "confirmed_by": period.confirmed_by if period else None,
            "override_reason": period.override_reason if period else None,
            "kpi": kpi,
            "daily": daily_list,
            "per_employee": per_employee_list,
        }

    # ── Audit log query ──────────────────────────────────────

    async def query_audit_logs(
        self,
        db: AsyncSession,
        *,
        store_id: Optional[UUID] = None,
        entity_type: Optional[str] = None,
        action: Optional[str] = None,
        actor_id: Optional[UUID] = None,
        limit: int = 200,
    ) -> list[dict]:
        """최근 audit log. store_id 필터는 entity 종류별 매핑으로 조인.

        - tip_entry: entry.store_id 매칭
        - tip_distribution: entry.store_id (via entry_id)
        - tip_period: period.store_id
        - form_4070: period.store_id (via period_id)
        """
        stmt = (
            select(TipAuditLog, User.full_name)
            .join(User, User.id == TipAuditLog.actor_id, isouter=True)
            .order_by(TipAuditLog.created_at.desc())
            .limit(limit)
        )
        if entity_type:
            stmt = stmt.where(TipAuditLog.entity_type == entity_type)
        if action:
            stmt = stmt.where(TipAuditLog.action == action)
        if actor_id:
            stmt = stmt.where(TipAuditLog.actor_id == actor_id)
        rows = (await db.execute(stmt)).all()
        if store_id is not None:
            # 클라가 store filter 보냈으면 entity_id → store_id 매핑 후 필터.
            # 4 entity_type 별 batch lookup.
            entry_ids = {l.entity_id for l, _ in rows if l.entity_type == "tip_entry"}
            dist_ids = {l.entity_id for l, _ in rows if l.entity_type == "tip_distribution"}
            period_ids = {l.entity_id for l, _ in rows if l.entity_type == "tip_period"}
            form_ids = {l.entity_id for l, _ in rows if l.entity_type == "form_4070"}

            store_by_entity: dict[UUID, UUID] = {}
            if entry_ids:
                for eid, sid in (await db.execute(
                    select(TipEntry.id, TipEntry.store_id).where(TipEntry.id.in_(entry_ids))
                )).all():
                    store_by_entity[eid] = sid
            if dist_ids:
                rs = (await db.execute(
                    select(TipDistribution.id, TipEntry.store_id)
                    .join(TipEntry, TipEntry.id == TipDistribution.entry_id)
                    .where(TipDistribution.id.in_(dist_ids))
                )).all()
                for did, sid in rs:
                    store_by_entity[did] = sid
            if period_ids:
                for pid, sid in (await db.execute(
                    select(TipPeriod.id, TipPeriod.store_id).where(TipPeriod.id.in_(period_ids))
                )).all():
                    store_by_entity[pid] = sid
            if form_ids:
                rs = (await db.execute(
                    select(Form4070Document.id, TipPeriod.store_id)
                    .join(TipPeriod, TipPeriod.id == Form4070Document.period_id)
                    .where(Form4070Document.id.in_(form_ids))
                )).all()
                for fid, sid in rs:
                    store_by_entity[fid] = sid

            rows = [
                (l, name) for l, name in rows
                if store_by_entity.get(l.entity_id) == store_id
            ]
        return [
            {
                "id": log.id,
                "entity_type": log.entity_type,
                "entity_id": log.entity_id,
                "action": log.action,
                "actor_id": log.actor_id,
                "actor_name": actor_name,
                "comment": log.comment,
                "before": log.before,
                "after": log.after,
                "created_at": log.created_at,
            }
            for log, actor_name in rows
        ]

    @staticmethod
    def build_entry_response(
        entry: TipEntry,
        *,
        store_name: Optional[str] = None,
        schedule: Optional[Schedule] = None,
        last_manager_note: Optional[str] = None,
        last_modified_by_name: Optional[str] = None,
    ) -> dict:
        dists = getattr(entry, "distributions", []) or []
        distributed_total = sum((Decimal(str(d.amount)) for d in dists), Decimal("0"))
        reportable_card = Decimal(str(entry.card_tips)) - distributed_total
        reported_on_4070 = Decimal(str(entry.cash_tips_kept)) + reportable_card

        # schedule 시간 — schedule 객체가 들어오면 fresh 시간 표시.
        schedule_start = None
        schedule_end = None
        if schedule is not None:
            schedule_start = schedule.start_time.isoformat(timespec="minutes") if schedule.start_time else None
            schedule_end = schedule.end_time.isoformat(timespec="minutes") if schedule.end_time else None

        return {
            "id": entry.id,
            "schedule_id": entry.schedule_id,
            "schedule_start_time": schedule_start,
            "schedule_end_time": schedule_end,
            "store_id": entry.store_id,
            "store_name": store_name,
            "employee_id": entry.employee_id,
            "work_role_id": entry.work_role_id,
            "work_role_name": entry.work_role_name_snapshot,
            "date": entry.date,
            "card_tips": entry.card_tips,
            "cash_tips_kept": entry.cash_tips_kept,
            "source": entry.source,
            "last_modified_by_id": entry.last_modified_by_id,
            "last_modified_at": entry.last_modified_at,
            "last_manager_note": last_manager_note,
            "last_modified_by_name": last_modified_by_name,
            "created_at": entry.created_at,
            "updated_at": entry.updated_at,
            "distributions": [
                {
                    "id": d.id,
                    "entry_id": d.entry_id,
                    "receiver_id": d.receiver_id,
                    "receiver_name": d.receiver_name_snapshot,
                    "amount": d.amount,
                    "reason": d.reason,
                    "status": d.status,
                    "pending_until": d.pending_until,
                    "accepted_at": d.accepted_at,
                    "created_at": d.created_at,
                }
                for d in dists
            ],
            "distributed_total": distributed_total,
            "reportable_card": reportable_card,
            "reported_on_4070": reported_on_4070,
        }


tip_service = TipService()
