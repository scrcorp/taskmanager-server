"""Attendance Device 서비스 — 매장 공용 기기 등록 + PIN 기반 clock in/out.

Attendance Device service layer — Handles terminal registration, token
verification, store assignment, and PIN-based clock operations that a
shared store device performs on behalf of any staff member.
"""

from __future__ import annotations

import hashlib
import secrets
import string
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.attendance import Attendance
from app.models.attendance_break import (
    VALID_BREAK_TYPES,
    PAID_BREAK_TYPES,
    UNPAID_BREAK_TYPES,
    AttendanceBreak,
    normalize_break_type,
)
from app.models.attendance_device import AttendanceDevice
from app.models.organization import Store
from app.models.user import User
from app.repositories.attendance_repository import attendance_repository
from app.utils.exceptions import BadRequestError, NotFoundError, UnauthorizedError

# clock action 타입
ClockAction = Literal["clock_in", "break_start", "break_end", "clock_out"]

# device_name 에 사용할 영숫자 (혼동 문자 제외)
_NAME_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@dataclass
class IdentifyContext:
    """identify_user_by_pin 의 typed 반환값.

    today_status / current_break / scheduled_end 는 device 가 store 미할당이거나
    오늘 attendance 가 없으면 None. (primary attendance 기준)
    today_attendances 는 오늘 모든 attendance(=schedule) 목록 (Issue 8 다중 schedule).
    """
    user: User
    today_status: str | None = None
    current_break: dict | None = None  # {break_type, started_at}
    scheduled_end: datetime | None = None
    today_attendances: list[dict] = field(default_factory=list)
    stale_attendances: list[dict] = field(default_factory=list)  # Issue 11


def generate_device_token() -> str:
    """URL-safe 32바이트 랜덤 토큰 (무기한). 기기에 1회만 반환, DB 에는 해시."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """sha256 hex digest — DB 저장용."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_device_name(suffix_length: int = 4) -> str:
    """기본 기기 이름 생성. 예: 'Terminal-A7K3'."""
    suffix = "".join(secrets.choice(_NAME_ALPHABET) for _ in range(suffix_length))
    return f"Terminal-{suffix}"


def generate_clockin_pin() -> str:
    """6자리 숫자 PIN 생성 (random, uniqueness 미보장).

    호출자가 commit 시 IntegrityError 처리해야 함 — `commit_pin_or_409` 사용.
    충돌 확률 1/1,000,000 이라 단일 호출 시 거의 발생 안 함.
    Bulk 케이스(마이그레이션 등) 에선 set 채우기 방식으로 사전 회피.
    """
    return f"{secrets.randbelow(1_000_000):06d}"


async def commit_pin_or_409(db: AsyncSession) -> None:
    """commit. `uq_user_org_clockin_pin` 위반 시 409 'Not available' 로 변환.

    그 외 IntegrityError 는 그대로 raise.
    """
    from fastapi import HTTPException, status
    from sqlalchemy.exc import IntegrityError

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if "uq_user_org_clockin_pin" in str(exc.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Not available"
            ) from exc
        raise


class AttendanceDeviceService:
    """attendance device 비즈니스 로직 모음."""

    # ── 등록 / 조회 / 해제 ─────────────────────────────────

    async def register(
        self,
        db: AsyncSession,
        organization_id: UUID,
        fingerprint: str | None = None,
    ) -> tuple[AttendanceDevice, str]:
        """새 기기 등록 — 랜덤 이름/토큰 발급. 평문 token 은 이 호출에서만 반환."""
        token = generate_device_token()
        device = AttendanceDevice(
            organization_id=organization_id,
            store_id=None,
            device_name=generate_device_name(),
            token_hash=hash_token(token),
            fingerprint=fingerprint,
            registered_at=datetime.now(timezone.utc),
        )
        db.add(device)
        await db.flush()
        return device, token

    async def get_by_token(
        self, db: AsyncSession, token: str
    ) -> AttendanceDevice | None:
        """평문 토큰 → 기기 조회 (revoke 시 row 삭제되므로 추가 필터 불필요)."""
        token_hash = hash_token(token)
        result = await db.execute(
            select(AttendanceDevice).where(
                AttendanceDevice.token_hash == token_hash,
            )
        )
        return result.scalar_one_or_none()

    async def touch_last_seen(self, db: AsyncSession, device: AttendanceDevice) -> None:
        """매 인증 성공 시 last_seen_at 갱신."""
        device.last_seen_at = datetime.now(timezone.utc)
        await db.flush()

    async def assign_store(
        self, db: AsyncSession, device: AttendanceDevice, store_id: UUID
    ) -> AttendanceDevice:
        """기기에 매장 할당/변경. 동일 조직 내 매장이어야 함.

        매장이 할당되면 `device_name` 을 store code (또는 name 앞 두 글자) 기반의
        순번 이름으로 재설정한다. 예: store.code='NB' → 'NB001', 'NB002'. code 가
        없으면 store.name 앞 두 글자 대문자 사용 (Hollywood → 'HO001').
        """
        from sqlalchemy import func as _func

        result = await db.execute(
            select(Store).where(
                Store.id == store_id,
                Store.organization_id == device.organization_id,
            )
        )
        store = result.scalar_one_or_none()
        if store is None:
            raise NotFoundError("Store not found in this organization")
        device.store_id = store_id

        # prefix 결정: store.code 우선, 없으면 store.name 앞 두 글자 대문자
        prefix: str | None = None
        if store.code:
            prefix = store.code.strip() or None
        if not prefix:
            base = (store.name or "").strip()
            if len(base) >= 2:
                prefix = base[:2].upper()
            elif len(base) == 1:
                prefix = base.upper()
        # 매장명이 공백이거나 비어있으면 fallback — 기존 이름 유지
        if prefix:
            # 같은 store 의 기기 수 (자기 자신 제외)
            count_stmt = (
                select(_func.count(AttendanceDevice.id))
                .where(
                    AttendanceDevice.store_id == store_id,
                    AttendanceDevice.id != device.id,
                )
            )
            count = (await db.execute(count_stmt)).scalar_one() or 0
            device.device_name = f"{prefix}{(count + 1):03d}"
        await db.flush()
        return device

    async def rename(
        self, db: AsyncSession, device: AttendanceDevice, new_name: str
    ) -> AttendanceDevice:
        name = (new_name or "").strip()
        if not name:
            raise BadRequestError("device_name is required")
        if len(name) > 100:
            raise BadRequestError("device_name too long (max 100)")
        device.device_name = name
        await db.flush()
        return device

    async def revoke(self, db: AsyncSession, device: AttendanceDevice) -> None:
        """해제 — row 즉시 삭제. 감사 이력 보존 안 함."""
        await db.delete(device)
        await db.flush()

    async def list_for_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> list[AttendanceDevice]:
        stmt = (
            select(AttendanceDevice)
            .where(AttendanceDevice.organization_id == organization_id)
            .order_by(AttendanceDevice.registered_at.desc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_manage(
        self,
        db: AsyncSession,
        organization_id: UUID,
        device_id: UUID,
    ) -> AttendanceDevice:
        result = await db.execute(
            select(AttendanceDevice).where(
                AttendanceDevice.id == device_id,
                AttendanceDevice.organization_id == organization_id,
            )
        )
        device = result.scalar_one_or_none()
        if device is None:
            raise NotFoundError("Device not found")
        return device

    # ── User + PIN 검증 ────────────────────────────────────

    async def _get_active_user(
        self, db: AsyncSession, user_id: UUID, organization_id: UUID
    ) -> User:
        """PIN 검증 없이 active user 조회 (manage override 전용)."""
        result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(
                User.id == user_id,
                User.organization_id == organization_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise BadRequestError("User not found")
        return user

    async def perform_clock_action_manage(
        self,
        db: AsyncSession,
        device: AttendanceDevice,
        action: ClockAction,
        user_id: UUID,
        manager_user_id: UUID,
        break_type: str | None = None,
        reason: str | None = None,
    ) -> Attendance:
        """매니저가 manage 모드에서 임의 사용자 attendance 를 처리.

        PIN 우회. early clock-in/out 가드 우회. note 에 manager 표시.
        """
        return await self.perform_clock_action(
            db,
            device=device,
            pin="",  # ignored
            action=action,
            user_id=user_id,
            break_type=break_type,
            reason=reason,
            skip_pin_check=True,
            skip_early_guards=True,
            manager_user_id=manager_user_id,
        )

    async def verify_user_pin(
        self, db: AsyncSession, user_id: UUID, pin: str, organization_id: UUID
    ) -> User:
        """user_id 로 유저를 조회 후 PIN 이 일치하는지 확인.

        기존 PIN → user 매핑 대신 user + PIN 검증 방식. 유저 없음/PIN 불일치
        모두 400 (device token 은 유효하므로 401 로 반환하지 않는다).
        """
        if not pin or not pin.isdigit() or not (4 <= len(pin) <= 6):
            raise BadRequestError("PIN must be 4-6 digits")
        result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(
                User.id == user_id,
                User.organization_id == organization_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            # 401 은 device token 문제에만 쓰고, 유저/PIN 오류는 400.
            raise BadRequestError("User not found")
        if user.clockin_pin != pin:
            raise BadRequestError("Invalid PIN")
        return user

    async def identify_manager_by_pin(
        self,
        db: AsyncSession,
        organization_id: UUID,
        pin: str,
    ) -> User:
        """매니저 진입용: PIN 으로 organization 안 active user 식별.

        identify_user_by_pin 과 비슷하지만 attendance context 계산 없이 User 만 반환.
        매니저 자격(SV+) 검증은 호출자가 수행.
        """
        if not pin or not pin.isdigit() or not (4 <= len(pin) <= 6):
            raise BadRequestError("PIN must be 4-6 digits")
        result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(
                User.organization_id == organization_id,
                User.clockin_pin == pin,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise BadRequestError("Invalid PIN")
        return user

    async def identify_user_by_pin(
        self,
        db: AsyncSession,
        pin: str,
        device: AttendanceDevice,
    ) -> "IdentifyContext":
        """PIN 단독으로 device 의 org 내 user 식별 + 오늘 attendance context 반환.

        PIN-first 키오스크 흐름 entry point (Phase 3 + Stage J 확장). 직원이 PIN
        입력하면 본인 식별 + 오늘 스케줄 있으면 today_status / current_break /
        scheduled_end 반환. 스케줄 없으면 셋 다 None.

        verify_user_pin 과 달리 user_id 필요 없음 — `(organization_id, clockin_pin)` unique
        제약 (Phase 1) 으로 단일 row 식별 가능.

        매니저 권한 / manage 모드 진입 검증은 본 endpoint 에 포함하지 않음 (Phase 6 에서 별도).
        """
        # 1. PIN 형식 — Stage J 부터 4~6자리 가변.
        if not pin or not pin.isdigit() or not (4 <= len(pin) <= 6):
            raise BadRequestError("PIN must be 4-6 digits")

        # 2. user 조회 — org/active/non-deleted, PIN 일치
        result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(
                User.organization_id == device.organization_id,
                User.clockin_pin == pin,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise BadRequestError("Invalid PIN")

        # 3. context 계산 — device 에 store 없으면 today_status / current_break /
        #    scheduled_end 모두 None.
        if device.store_id is None:
            return IdentifyContext(user=user)

        return await self._compute_identify_context_for_user(
            db,
            user=user,
            store_id=device.store_id,
            organization_id=device.organization_id,
        )

    async def _compute_identify_context_for_user(
        self,
        db: AsyncSession,
        user: User,
        store_id: UUID,
        organization_id: UUID,
    ) -> "IdentifyContext":
        """store work_date 기준 오늘 user 의 attendance context 계산.

        (Issue 8) 한 직원이 같은 날 2+ schedule 을 가질 수 있으므로 모든 row 를 가져와
        우선순위로 정렬한 list 를 반환. primary (정렬 첫 번째) 가 today_status 등 단일
        필드 채움 (단일 schedule 케이스 호환).

        반환 dataclass 의 필드:
          - today_status: primary attendance 의 effective status. 스케줄 없으면 None.
          - current_break: primary 가 on_break 일 때 (break_type, started_at) dict.
          - scheduled_end: primary schedule end_time → store TZ aware UTC.
          - today_attendances: 오늘 모든 attendance dict 목록 (우선순위 정렬).
        """
        from zoneinfo import ZoneInfo

        from app.models.schedule import Schedule
        from app.services.attendance_service import compute_effective_status
        from app.utils.settings_resolver import SettingNotRegisteredError, resolve_setting
        from app.utils.timezone import get_store_day_config, get_work_date

        now = datetime.now(timezone.utc)
        store_tz, store_day_start = await get_store_day_config(db, store_id)
        today = get_work_date(store_tz, store_day_start, now)
        tz_info = ZoneInfo(store_tz)

        def _tz_hhmm(value: datetime | None) -> str | None:
            return value.astimezone(tz_info).strftime("%H:%M") if value else None

        # (Issue 11) 이전 work_date 미완료(orphan) — 오늘 attendance 유무와 무관하게
        # 항상 조회 (오늘 schedule 없어도 어제 미완료 있으면 경고). 최근 30일, 기기 매장.
        from datetime import timedelta as _td_stale
        stale_rows = list(
            (
                await db.execute(
                    select(Attendance.work_date, Attendance.status, Attendance.clock_in)
                    .where(
                        Attendance.user_id == user.id,
                        Attendance.store_id == store_id,
                        Attendance.clock_in.isnot(None),
                        Attendance.clock_out.is_(None),
                        Attendance.status.in_(["working", "on_break", "late"]),
                        Attendance.work_date < today,
                        Attendance.work_date >= today - _td_stale(days=30),
                    )
                    .order_by(Attendance.work_date.desc())
                )
            ).all()
        )
        stale = [
            {"work_date": wd, "status": st, "clock_in_display": _tz_hhmm(ci)}
            for (wd, st, ci) in stale_rows
        ]

        rows = list(
            (
                await db.execute(
                    select(Attendance, Schedule)
                    .outerjoin(Schedule, Schedule.id == Attendance.schedule_id)
                    .where(
                        Attendance.user_id == user.id,
                        Attendance.store_id == store_id,
                        Attendance.work_date == today,
                        Attendance.status != "cancelled",
                    )
                )
            ).all()
        )
        if not rows:
            return IdentifyContext(user=user, stale_attendances=stale)

        try:
            late_buf_raw = await resolve_setting(
                db,
                key="attendance.late_buffer_minutes",
                organization_id=organization_id,
                store_id=store_id,
            )
            late_buffer = int(late_buf_raw) if late_buf_raw is not None else 5
        except (SettingNotRegisteredError, TypeError, ValueError):
            late_buffer = 5

        def _display(value: datetime | None) -> str | None:
            if value is None:
                return None
            return value.astimezone(tz_info).strftime("%H:%M")

        # 각 row → item dict (effective status + scheduled times + current_break)
        items: list[dict] = []
        for att, schedule in rows:
            eff_status = compute_effective_status(
                att_status=att.status,
                att_clock_in=att.clock_in,
                schedule_start_time=schedule.start_time if schedule else None,
                schedule_end_time=schedule.end_time if schedule else None,
                schedule_work_date=schedule.work_date if schedule else None,
                now=now,
                store_tz=tz_info,
                late_buffer=late_buffer,
            )

            sched_start_utc: datetime | None = None
            sched_end_utc: datetime | None = None
            if schedule and schedule.work_date is not None:
                if schedule.start_time is not None:
                    sched_start_utc = datetime.combine(
                        schedule.work_date, schedule.start_time
                    ).replace(tzinfo=tz_info).astimezone(timezone.utc)
                if schedule.end_time is not None:
                    sched_end_utc = datetime.combine(
                        schedule.work_date, schedule.end_time
                    ).replace(tzinfo=tz_info).astimezone(timezone.utc)

            cur_break: dict | None = None
            if eff_status == "on_break":
                br = (
                    await db.execute(
                        select(AttendanceBreak)
                        .where(
                            AttendanceBreak.attendance_id == att.id,
                            AttendanceBreak.ended_at.is_(None),
                        )
                        .order_by(AttendanceBreak.started_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if br is not None:
                    cur_break = {
                        "break_type": br.break_type,
                        "started_at": br.started_at,
                    }

            items.append({
                "schedule_id": att.schedule_id,
                "status": eff_status,
                "scheduled_start": sched_start_utc,
                "scheduled_end": sched_end_utc,
                "scheduled_start_display": _display(sched_start_utc),
                "scheduled_end_display": _display(sched_end_utc),
                "current_break": cur_break,
            })

        # 우선순위 정렬: working > on_break > late > soon > upcoming > no_show > clocked_out
        rank = {
            "working": 0, "on_break": 1, "late": 2, "soon": 3,
            "upcoming": 4, "no_show": 5, "clocked_out": 6,
        }
        items.sort(key=lambda it: (
            rank.get(it["status"], 99),
            it["scheduled_start"] or datetime.max.replace(tzinfo=timezone.utc),
        ))

        primary = items[0]
        return IdentifyContext(
            user=user,
            today_status=primary["status"],
            current_break=primary["current_break"],
            scheduled_end=primary["scheduled_end"],
            today_attendances=items,
            stale_attendances=stale,
        )

    # ── Clock 동작 ─────────────────────────────────────────

    async def _get_open_break(
        self, db: AsyncSession, attendance_id: UUID
    ) -> AttendanceBreak | None:
        """해당 attendance 의 아직 닫히지 않은 break 1건 조회."""
        result = await db.execute(
            select(AttendanceBreak)
            .where(
                AttendanceBreak.attendance_id == attendance_id,
                AttendanceBreak.ended_at.is_(None),
            )
            .order_by(AttendanceBreak.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def perform_clock_action(
        self,
        db: AsyncSession,
        device: AttendanceDevice,
        pin: str,
        action: ClockAction,
        user_id: UUID,
        break_type: str | None = None,
        reason: str | None = None,
        skip_pin_check: bool = False,
        skip_early_guards: bool = False,
        manager_user_id: UUID | None = None,
        schedule_id: UUID | None = None,
    ) -> Attendance:
        """기기 + user_id + PIN 으로 clock in/out/break 처리.

        break 는 attendance_breaks 테이블에 행 단위로 기록. break-start 는
        break_type 필수 (paid_10min | unpaid_meal). 같은 attendance 에 여러 번
        휴식 가능하며 open 상태 (ended_at IS NULL) 는 1건만 허용.

        Admin override 모드 (skip_pin_check=True) 는 매니저가 키오스크 관리자 모드에서
        타인 attendance 를 처리할 때 사용. PIN 우회 + early in/out 가드 우회.
        """
        if device.store_id is None:
            raise BadRequestError("Device has no store assigned")

        if skip_pin_check:
            user = await self._get_active_user(db, user_id, device.organization_id)
        else:
            user = await self.verify_user_pin(db, user_id, pin, device.organization_id)
        store_id = device.store_id
        now = datetime.now(timezone.utc)

        # 타임존/work_date 결정 — 매장 기준
        from app.utils.timezone import get_store_day_config, get_work_date

        store_tz, store_day_start = await get_store_day_config(db, store_id)
        today: date = get_work_date(store_tz, store_day_start, now)

        # Split shift 대응 — 하루 여러 row 가 있을 수 있으므로 list 로 조회.
        day_rows = await attendance_repository.list_user_day(db, user.id, today)

        # clock-in 외 액션(break/clock_out)은 "지금 활성" row 기준.
        # working → on_break → late 순으로 찾고, 없으면 None.
        def _active_row() -> Attendance | None:
            for target_status in ("working", "on_break", "late"):
                for r in day_rows:
                    if r.status == target_status:
                        return r
            return None

        attendance = _active_row() if action != "clock_in" else (day_rows[0] if day_rows else None)

        if action == "clock_in":
            # 1) 실제로 출근중인 shift(clock_in 있고 clock_out 없는 working/on_break) 만 차단.
            #    late는 "스케줄 지났는데 미출근" 상태일 수 있어 clock_in 여부로 판단해야 한다 —
            #    이전 shift가 단순 미출근(late, clock_in IS NULL)이면 새 shift clock-in 허용.
            active = next(
                (r for r in day_rows
                 if r.clock_in is not None and r.clock_out is None
                 and r.status in ("working", "on_break", "late")),
                None,
            )
            if active is not None:
                raise BadRequestError("Previous shift not clocked out. Clock out first.")

            # 2) clock-in 대상 schedule 선택 — 이 매장/유저/오늘 confirmed 중
            #    "아직 clock_in 안 된" attendance row 와 묶인 것만 후보.
            from app.models.schedule import Schedule
            from datetime import timedelta as _td
            from zoneinfo import ZoneInfo as _Zi

            sch_result = await db.execute(
                select(Schedule)
                .where(
                    Schedule.user_id == user.id,
                    Schedule.store_id == store_id,
                    Schedule.work_date == today,
                    Schedule.status == "confirmed",
                )
                .order_by(Schedule.start_time.asc().nulls_last())
            )
            all_candidates = list(sch_result.scalars().all())
            if not all_candidates:
                raise BadRequestError("No scheduled shift for today at this store")

            # 이미 끝난(clocked_out) shift 는 후보에서 제외. 같은 schedule_id 에
            # attendance 가 clocked_out 인 경우 재출근 금지.
            done_schedule_ids = {r.schedule_id for r in day_rows if r.status == "clocked_out" and r.schedule_id is not None}
            candidates = [s for s in all_candidates if s.id not in done_schedule_ids]
            if not candidates:
                raise BadRequestError("All today's shifts are already completed")

            tz = _Zi(store_tz)

            def _start_dt(s):
                if s.start_time is None:
                    return None
                return datetime.combine(today, s.start_time, tzinfo=tz)

            def _end_dt(s):
                if s.end_time is None:
                    return None
                return datetime.combine(today, s.end_time, tzinfo=tz)

            schedule = None
            # (Issue 8) client 가 명시적으로 schedule 을 선택한 경우 그것을 사용.
            # 단 candidates (= clock-in 가능한 미완료 shift) 에 있어야 함.
            # clocked_out 등으로 candidates 에 없으면 명시 거부 (우선순위 fallback 안 함).
            if schedule_id is not None:
                schedule = next((s for s in candidates if s.id == schedule_id), None)
                if schedule is None:
                    raise BadRequestError(
                        "Selected shift is not available for clock-in"
                    )
            # 우선순위 1: 현재 window (start <= now <= end) 안에 있는 스케줄
            if schedule is None:
                for s in candidates:
                    sd = _start_dt(s)
                    ed = _end_dt(s)
                    if sd is not None and ed is not None and sd <= now <= ed:
                        schedule = s
                        break
            # 우선순위 2: 가장 가까운 미래 (start > now)
            if schedule is None:
                future = [s for s in candidates if (_start_dt(s) or datetime.min.replace(tzinfo=tz)) > now]
                if future:
                    future.sort(key=lambda s: _start_dt(s) or datetime.max.replace(tzinfo=tz))
                    schedule = future[0]
            # 우선순위 3: 가장 최근 종료 (end < now)
            if schedule is None:
                past = [s for s in candidates if (_end_dt(s) or datetime.max.replace(tzinfo=tz)) < now]
                if past:
                    past.sort(key=lambda s: _end_dt(s) or datetime.min.replace(tzinfo=tz), reverse=True)
                    schedule = past[0]
            if schedule is None:
                schedule = candidates[0]

            # late 판정 — clock_in > scheduled_start + LATE_BUFFER
            from app.services.attendance_service import LATE_BUFFER_MINUTES
            from app.utils.settings_resolver import (
                SettingNotRegisteredError,
                resolve_setting,
            )

            status_val = "working"
            anomalies: list[str] | None = None
            if schedule.start_time is not None:
                scheduled_start = datetime.combine(today, schedule.start_time, tzinfo=tz)
                # Early clock-in threshold — 너무 일찍 clock-in 시도 차단.
                try:
                    raw = await resolve_setting(
                        db,
                        key="attendance.early_clock_in_threshold_minutes",
                        organization_id=device.organization_id,
                        store_id=store_id,
                    )
                    early_threshold = int(raw) if raw is not None else 5
                except (SettingNotRegisteredError, TypeError, ValueError):
                    early_threshold = 5
                if not skip_early_guards and now < scheduled_start - _td(minutes=early_threshold):
                    minutes_until = int(
                        (scheduled_start - now).total_seconds() / 60
                    )
                    raise BadRequestError(
                        f"Too early to clock in. Shift starts in {minutes_until} minutes."
                    )
                if now > scheduled_start + _td(minutes=LATE_BUFFER_MINUTES):
                    status_val = "late"
                    anomalies = ["late"]

            # Eager 모델: 이 schedule 에 묶인 attendance row 는 이미 존재해야 함.
            # upcoming/late/no_show 상태에서 clock-in 시 update.
            target = await attendance_repository.get_by_schedule_id(db, schedule.id)
            if target is not None:
                target.store_id = store_id
                target.clock_in = now
                target.clock_in_timezone = store_tz
                target.status = status_val
                existing_anoms = [a for a in (target.anomalies or []) if a != "no_show"]
                if anomalies:
                    for a in anomalies:
                        if a not in existing_anoms:
                            existing_anoms.append(a)
                target.anomalies = existing_anoms or None
                await db.flush()
                await db.refresh(target)
                attendance = target
            else:
                # 예외적인 경우 (eager 훅 누락 등) — 안전망으로 새 row 생성.
                attendance = await attendance_repository.create(
                    db,
                    {
                        "organization_id": device.organization_id,
                        "store_id": store_id,
                        "user_id": user.id,
                        "schedule_id": schedule.id,
                        "work_date": today,
                        "clock_in": now,
                        "clock_in_timezone": store_tz,
                        "status": status_val,
                        "anomalies": anomalies,
                    },
                )
        elif action == "break_start":
            if attendance is None:
                raise BadRequestError("Must clock in first")
            if attendance.status not in ("working", "late"):
                raise BadRequestError("Cannot start break in current state")
            if break_type not in VALID_BREAK_TYPES:
                raise BadRequestError(
                    "break_type required (paid_10min or unpaid_meal)"
                )
            open_break = await self._get_open_break(db, attendance.id)
            if open_break is not None:
                raise BadRequestError("A break is already in progress")
            new_break = AttendanceBreak(
                attendance_id=attendance.id,
                started_at=now,
                break_type=normalize_break_type(break_type),
            )
            db.add(new_break)
            attendance.status = "on_break"
            # 하위호환: 기존 컬럼도 최근 break 기준으로 갱신
            attendance.break_start = now
            attendance.break_end = None
            await db.flush()
            await db.refresh(attendance)
        elif action == "break_end":
            if attendance is None:
                raise BadRequestError("Must clock in first")
            if attendance.status != "on_break":
                raise BadRequestError("Not currently on break")
            open_break = await self._get_open_break(db, attendance.id)
            if open_break is None:
                # 상태는 on_break 인데 open row 가 없음 (데이터 불일치) — 보정
                attendance.status = "working"
                await db.flush()
                raise BadRequestError("No open break record")

            # Stage J: break time 정책 검증 (pure helper)
            from app.utils.break_end_policy import validate_break_end
            elapsed_minutes = max(0, int((now - open_break.started_at).total_seconds() / 60))
            policy_error = validate_break_end(open_break.break_type, elapsed_minutes, reason)
            if policy_error is not None:
                raise BadRequestError(policy_error)

            open_break.ended_at = now
            open_break.duration_minutes = elapsed_minutes
            attendance.status = "working"
            attendance.break_end = now
            # 누적 분 — 새 테이블에서 합산
            total_minutes = await self._sum_break_minutes(db, attendance.id)
            attendance.total_break_minutes = total_minutes
            await db.flush()
            await db.refresh(attendance)
        elif action == "clock_out":
            if attendance is None:
                raise BadRequestError("Must clock in first")
            if attendance.status not in ("working", "late", "on_break"):
                raise BadRequestError("Already clocked out")

            # Early clock-out 검증 — schedule end 의 threshold 이전이면 reason 필수.
            from datetime import timedelta as _td2
            from zoneinfo import ZoneInfo as _Zi2
            from app.utils.settings_resolver import (
                SettingNotRegisteredError as _SNRE,
                resolve_setting as _resolve,
            )

            is_early = False
            sched_end_dt = None
            if attendance.schedule_id is not None:
                from app.models.schedule import Schedule as _Schedule
                _sch = await db.scalar(
                    select(_Schedule).where(_Schedule.id == attendance.schedule_id)
                )
                if _sch is not None and _sch.end_time is not None:
                    _tz_obj = _Zi2(store_tz)
                    sched_end_dt = datetime.combine(
                        _sch.work_date, _sch.end_time, tzinfo=_tz_obj
                    )
                    if _sch.start_time is not None:
                        _start_dt = datetime.combine(
                            _sch.work_date, _sch.start_time, tzinfo=_tz_obj
                        )
                        if sched_end_dt <= _start_dt:
                            sched_end_dt = sched_end_dt + _td2(days=1)
                    try:
                        _raw = await _resolve(
                            db,
                            key="attendance.early_leave_threshold_minutes",
                            organization_id=device.organization_id,
                            store_id=store_id,
                        )
                        _early_thresh = int(_raw) if _raw is not None else 5
                    except (_SNRE, TypeError, ValueError):
                        _early_thresh = 5
                    if now < sched_end_dt - _td2(minutes=_early_thresh):
                        is_early = True
            if not skip_early_guards and is_early and not (reason and reason.strip()):
                raise BadRequestError(
                    "Early clock-out requires a reason. Please provide one."
                )

            # 진행중 break 가 있으면 먼저 종료 처리
            if attendance.status == "on_break":
                open_break = await self._get_open_break(db, attendance.id)
                if open_break is not None:
                    open_break.ended_at = now
                    delta = now - open_break.started_at
                    open_break.duration_minutes = max(0, int(delta.total_seconds() / 60))
                    attendance.break_end = now
            attendance.clock_out = now
            attendance.clock_out_timezone = store_tz
            attendance.status = "clocked_out"
            if attendance.clock_in is not None:
                work_delta = now - attendance.clock_in
                attendance.total_work_minutes = int(work_delta.total_seconds() / 60)
            attendance.total_break_minutes = await self._sum_break_minutes(db, attendance.id)

            if is_early:
                anoms = list(attendance.anomalies or [])
                if "early_clock_out" not in anoms:
                    anoms.append("early_clock_out")
                attendance.anomalies = anoms or None
                # early-clock-out 사유는 attendance_corrections 에 기록 (note 더럽히지 않음).
                # 매니저가 console 에서 note 따로 메모하는 영역과 분리.

            await db.flush()
            await db.refresh(attendance)
        else:
            raise BadRequestError(f"Invalid action: {action}")

        # ── 모든 attendance 액션을 timeline 에 기록 ──
        # field_name 의미:
        #   - staff PIN 정상 액션  → action verb 그대로 (clock_in / clock_out / break_start / break_end)
        #   - admin override      → "modify" (매니저가 임의 사용자에 대해 직접 처리)
        # corrected_value 는 새 시각/status 등 핵심 값.
        from app.models.attendance import AttendanceCorrection
        actor_id = manager_user_id if skip_pin_check else user.id
        field_label = "modify" if skip_pin_check else action
        # corrected_value: clock_in/out/break_start/break_end 는 해당 시각 ISO,
        # modify 는 결과 status (이 액션의 효과).
        cv: str
        if skip_pin_check:
            cv = attendance.status
        elif action in ("clock_in", "clock_out"):
            t = getattr(attendance, action, None)
            cv = t.isoformat() if t else "(set)"
        elif action == "break_start":
            cv = (break_type or "break")
        elif action == "break_end":
            cv = "ended"
        else:
            cv = attendance.status
        user_reason = (reason or "").strip()
        if user_reason:
            correction_reason = user_reason
        else:
            correction_reason = None
        db.add(AttendanceCorrection(
            attendance_id=attendance.id,
            field_name=field_label,
            original_value=None,
            corrected_value=cv,
            reason=correction_reason or "(no reason)",
            corrected_by=actor_id,
        ))

        await self.touch_last_seen(db, device)
        try:
            await db.commit()
            return attendance
        except Exception:
            await db.rollback()
            raise

    async def _sum_break_minutes(
        self, db: AsyncSession, attendance_id: UUID
    ) -> int:
        """attendance 의 모든 종료된 break 분 합계."""
        result = await db.execute(
            select(AttendanceBreak.duration_minutes).where(
                AttendanceBreak.attendance_id == attendance_id,
                AttendanceBreak.duration_minutes.is_not(None),
            )
        )
        return sum((v or 0) for v, in result.all())

    async def get_break_summary(
        self, db: AsyncSession, attendance_id: UUID
    ) -> dict:
        """attendance 의 break 합계 (paid/unpaid 분리) + current open break."""
        result = await db.execute(
            select(AttendanceBreak).where(AttendanceBreak.attendance_id == attendance_id)
        )
        breaks = list(result.scalars().all())
        paid = sum(b.duration_minutes or 0 for b in breaks if b.break_type in PAID_BREAK_TYPES)
        unpaid = sum(
            b.duration_minutes or 0 for b in breaks if b.break_type in UNPAID_BREAK_TYPES
        )
        current = next((b for b in breaks if b.ended_at is None), None)
        return {
            "paid_minutes": paid,
            "unpaid_minutes": unpaid,
            "current": current,
            "all": breaks,
        }


attendance_device_service = AttendanceDeviceService()
