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

from sqlalchemy import literal, or_, select
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
from app.models.user_store import UserStore
from app.repositories.attendance_repository import attendance_repository
from app.utils.exceptions import BadRequestError, NotFoundError, UnauthorizedError
from app.utils.timezone import minutes_between, resolve_schedule_instants

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


async def generate_unique_clockin_pin(
    db: AsyncSession,
    organization_id: UUID,
    exclude_user_id: UUID | None = None,
    attempts: int = 10,
) -> str:
    """org 안에서 중복·prefix 충돌이 없는 6자리 PIN 생성.

    `generate_clockin_pin()` 은 uniqueness 를 보장하지 않는데, 4~6자리 가변 도입 후엔
    "기존 4자리 PIN 을 prefix 로 갖는 6자리" 가 뽑힐 수 있어 재시도가 필요하다.
    (예: 기존 `1234` 가 있으면 `123456` 은 뽑으면 안 된다)

    `attempts` 회 모두 실패하면 마지막 후보를 그대로 반환 — 최종 방어는
    unique 제약(`commit_pin_or_409`) 이다. org 당 PIN 수를 감안하면 실제로는
    1회차에 거의 끝난다.
    """
    from fastapi import HTTPException

    pin = generate_clockin_pin()
    for _ in range(attempts):
        try:
            await assert_no_pin_prefix_conflict(
                db, organization_id, pin, exclude_user_id
            )
            return pin
        except HTTPException:
            pin = generate_clockin_pin()
    return pin


async def assert_no_pin_prefix_conflict(
    db: AsyncSession,
    organization_id: UUID,
    pin: str,
    exclude_user_id: UUID | None = None,
    store_id: UUID | None = None,
) -> None:
    """org 안에서 PIN prefix 충돌을 막는다. 충돌 시 409 (구조화 detail).

    PIN 길이가 4~6 로 가변이라 `uq_user_org_clockin_pin`(정확 일치) 만으로는 부족하다.
    A=`1234`, B=`123456` 이 공존하면 B 가 앞 4자리만 누르고 확인을 눌렀을 때 A 로 식별돼
    **남의 이름으로 출퇴근이 찍힌다.** 그래서 다음 두 방향을 모두 거부한다.

        - 신규 PIN 이 기존 PIN 의 prefix        (new=`1234`,   기존=`123456`)
        - 기존 PIN 이 신규 PIN 의 prefix        (new=`123456`, 기존=`1234`)

    정확히 같은 값도 이 조건에 걸리므로 중복 검사까지 겸한다(선 검사 → 친절한 409,
    동시성으로 빠져나간 경우는 `commit_pin_or_409` 의 unique 위반이 최종 방어).

    409 detail 계약 (모든 클라이언트 공통):
        {"code": "pin_conflict", "reason": "exact"|"prefix",
         "other_store": true|false|null, "message": "<영어 사유 문장>"}

    - reason: 충돌 PIN 이 제출 PIN 과 정확히 같으면 exact,
      길이가 다른 두 PIN 이 앞자리를 공유(양방향)하면 prefix.
    - other_store: manage(키오스크) 경로에서만 채움 — `store_id` 가 주어지면
      충돌 유저가 그 매장(user_stores)에 없을 때 True. 그 외 경로는 None.
    - 타인의 PIN 값·이름은 어떤 필드에도 절대 싣지 않는다.
    """
    from fastapi import HTTPException, status

    stmt = (
        select(User.id, User.clockin_pin)
        .where(
            User.organization_id == organization_id,
            User.clockin_pin.isnot(None),
            or_(
                User.clockin_pin.startswith(pin),
                literal(pin).startswith(User.clockin_pin),
            ),
        )
        # exact 충돌을 우선 선택 — exact/prefix 충돌이 공존해도 reason 이 결정적이게.
        .order_by((User.clockin_pin == pin).desc())
    )
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)

    row = (await db.execute(stmt.limit(1))).first()
    if row is None:
        return

    conflict_user_id, conflict_pin = row
    reason = "exact" if conflict_pin == pin else "prefix"

    other_store: bool | None = None
    if store_id is not None:
        in_store = (
            await db.execute(
                select(UserStore.user_id)
                .where(
                    UserStore.user_id == conflict_user_id,
                    UserStore.store_id == store_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        other_store = in_store is None

    if reason == "prefix":
        message = (
            "This PIN overlaps with another employee's PIN "
            "(numbers that start the same)."
        )
    elif other_store is True:
        message = "This PIN is already in use by an employee at another store."
    else:
        message = "This PIN is already in use by another employee."

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "pin_conflict",
            "reason": reason,
            "other_store": other_store,
            "message": message,
        },
    )


async def commit_pin_or_409(db: AsyncSession) -> None:
    """commit. `uq_user_org_clockin_pin` 위반 시 409 pin_conflict 로 변환.

    unique 제약 fallback 이라 old-row 정보가 없다 — reason 은 exact 로 고정,
    other_store 는 null. 그 외 IntegrityError 는 그대로 raise.
    """
    from fastapi import HTTPException, status
    from sqlalchemy.exc import IntegrityError

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if "uq_user_org_clockin_pin" in str(exc.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "pin_conflict",
                    "reason": "exact",
                    "other_store": None,
                    "message": "This PIN is already in use by another employee.",
                },
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
                schedule_start_at=schedule.start_at if schedule else None,
                schedule_end_at=schedule.end_at if schedule else None,
            )

            sched_start_utc: datetime | None = None
            sched_end_utc: datetime | None = None
            if schedule is not None:
                _ss, _se = resolve_schedule_instants(
                    start_at=schedule.start_at, end_at=schedule.end_at,
                    work_date=schedule.work_date, start_time=schedule.start_time,
                    end_time=schedule.end_time, tz_name=tz_info.key,
                )
                sched_start_utc = _ss.astimezone(timezone.utc) if _ss else None
                sched_end_utc = _se.astimezone(timezone.utc) if _se else None

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
        walk_in: bool = False,
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
        # 새벽 근무(라벨=전날 영업일)가 경계(day_start)를 넘겨 아직 진행 중일 수 있다 —
        # break/clock-out 과 이중 clock-in 가드는 전날 라벨의 "열린"(clock_in 있고
        # clock_out 없는) row 도 봐야 한다. 안 그러면 01:00~09:00 새벽조가 06:00 경계를
        # 넘는 순간 퇴근/휴식이 전부 불가능해진다.
        from datetime import timedelta as _td_prev
        prev_rows = await attendance_repository.list_user_day(db, user.id, today - _td_prev(days=1))
        open_prev = [
            r for r in prev_rows
            if r.clock_in is not None and r.clock_out is None
        ]
        rows_ext = list(day_rows) + open_prev

        # clock-in 외 액션(break/clock_out)은 "지금 활성" row 기준.
        # working → on_break → late 순으로 찾고, 없으면 None.
        def _active_row() -> Attendance | None:
            for target_status in ("working", "on_break", "late"):
                for r in rows_ext:
                    if r.status == target_status:
                        return r
            return None

        attendance = _active_row() if action != "clock_in" else (day_rows[0] if day_rows else None)

        # ── 타임라인용 before 캡처 ──
        # 각 분기가 실제로 건드리는 row 를 기준으로 변경 직전 값을 잡아둔다.
        # 여기서 안 잡으면 "이전 상태"를 영영 복원할 수 없다 (기존 버그의 원인).
        before_status: str | None = None
        before_clock_in: datetime | None = None
        before_clock_out: datetime | None = None
        touched_break: AttendanceBreak | None = None
        break_before: tuple[datetime | None, datetime | None, str | None] = (None, None, None)

        if action == "clock_in":
            # 1) 실제로 출근중인 shift(clock_in 있고 clock_out 없는 working/on_break) 만 차단.
            #    late는 "스케줄 지났는데 미출근" 상태일 수 있어 clock_in 여부로 판단해야 한다 —
            #    이전 shift가 단순 미출근(late, clock_in IS NULL)이면 새 shift clock-in 허용.
            active = next(
                (r for r in rows_ext
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
                    Schedule.operating_day == today,
                    Schedule.status == "confirmed",
                )
                .order_by(Schedule.start_at.asc().nulls_last())
            )
            all_candidates = list(sch_result.scalars().all())

            # 이미 끝난(clocked_out) shift 는 후보에서 제외.
            done_schedule_ids = {r.schedule_id for r in day_rows if r.status == "clocked_out" and r.schedule_id is not None}
            candidates = [s for s in all_candidates if s.id not in done_schedule_ids]

            if not candidates:
                # 사용할 수 있는 "열린" 스케줄이 없음. 매장이 walk_in 을 허용하고 요청이
                # 워크인 의도이면, 오늘 스케줄이 아예 없든 / 이전 (워크인)shift 가 모두
                # clocked_out 이든 관계없이 **새 워크인 스케줄을 생성**한다. → 퇴근 후
                # 다시 출근(하루 여러 shift) 가능. (열린 shift 가 남아있으면 위 active 가드가
                # 먼저 막으므로, 여기 도달했다는 건 열린 shift 가 없다는 뜻.)
                from app.utils.settings_resolver import (
                    SettingNotRegisteredError as _SNRE_wi,
                    resolve_setting as _resolve_wi,
                )

                walk_in_allowed = False
                if walk_in:
                    try:
                        walk_in_allowed = bool(
                            await _resolve_wi(
                                db,
                                key="attendance.walk_in_allowed",
                                organization_id=device.organization_id,
                                store_id=store_id,
                            )
                        )
                    except _SNRE_wi:
                        walk_in_allowed = False

                if walk_in_allowed:
                    from app.services.schedule_service import schedule_service
                    walk_in_schedule = await schedule_service.create_walk_in_schedule(
                        db,
                        organization_id=device.organization_id,
                        store_id=store_id,
                        user_id=user.id,
                        work_date=today,
                        clock_in_at=now,
                        store_tz=store_tz,
                        created_by=user.id,
                    )
                    candidates = [walk_in_schedule]
                    # 방금 생성한 워크인 스케줄을 사용한다. 클라가 이전 (clocked_out)
                    # shift 의 schedule_id 를 실어 보냈더라도 그건 무시 — 안 그러면
                    # 아래 명시-선택 체크에서 새 스케줄과 불일치로 거부된다.
                    schedule_id = None
                elif not all_candidates:
                    raise BadRequestError("No scheduled shift for today at this store")
                else:
                    raise BadRequestError("All today's shifts are already completed")

            tz = _Zi(store_tz)

            def _instants(s):
                return resolve_schedule_instants(
                    start_at=s.start_at, end_at=s.end_at, work_date=s.work_date,
                    start_time=s.start_time, end_time=s.end_time, tz_name=tz.key,
                )

            def _start_dt(s):
                return _instants(s)[0]

            def _end_dt(s):
                return _instants(s)[1]

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

            from app.services.attendance_service import (
                ANOMALY_EARLY_CLOCK_IN_OVERRIDE,
            )

            status_val = "working"
            anomalies: list[str] | None = None
            # 조기 출근 강행 여부 + "이미 승인된 건인가"(매니저 대행) — 확인 게이트용.
            early_clock_in_override = False
            early_override_preapproved = False
            scheduled_start = _start_dt(schedule)
            if scheduled_start is not None:
                # Early clock-in threshold — 이보다 이르면 사유 없이는 못 찍는다.
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
                # late/early 판정은 분 단위로만 한다(초는 버림). clock_in 은 초까지 저장하되
                # "정시 출근(같은 분)"이 초 차이로 late 로 찍히지 않게 한다. 워크인은 start=
                # clock-in(분 내림)이라 이 규칙으로 자연히 early/late 가 아니게 된다(특수예외 불필요).
                now_min = now.replace(second=0, microsecond=0)
                # 조기 clock-in override — 예정보다 이르면 차단이 아니라 "사유 요구".
                # 매니저/SV 가 현장에 없어도 직원이 직접 찍을 수 있어야 하기 때문
                # (일찍 와달라고 부른 경우). 사유가 오면 통과시키고 anomaly 로 표시한다.
                # 상한(몇 시간 전까지) 은 두지 않는다 — 얼마나 일찍 부를지 예측 불가.
                #
                # 매니저 대행(skip_early_guards) 도 **라벨은 똑같이 붙인다** — 예정 밖
                # 근무는 특이사항으로 보여야 하기 때문. 다만 사유는 요구하지 않고,
                # 확인이 이미 끝난 것으로 처리한다(매니저가 그 자리에서 승인한 행위라
                # 다시 확인시키면 이중 확인). 자동 확인은 Phase 2 의 확인 컬럼이 담당.
                if now_min < scheduled_start - _td(minutes=early_threshold):
                    early_clock_in_override = True
                    early_override_preapproved = skip_early_guards
                    if not skip_early_guards and not (reason and reason.strip()):
                        from fastapi import HTTPException, status

                        minutes_early = int(
                            (scheduled_start - now_min).total_seconds() / 60
                        )
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail={
                                "code": "early_clock_in_reason_required",
                                "minutes_early": minutes_early,
                                "schedule_id": str(schedule.id),
                                "scheduled_start": scheduled_start.isoformat(),
                                "message": (
                                    "This shift starts in "
                                    f"{minutes_early} minutes. To clock in now, "
                                    "enter a reason — your manager will see it."
                                ),
                            },
                        )
                    anomalies = [ANOMALY_EARLY_CLOCK_IN_OVERRIDE]
                if now_min > scheduled_start + _td(minutes=LATE_BUFFER_MINUTES):
                    status_val = "late"
                    anomalies = ["late"]

            # Eager 모델: 이 schedule 에 묶인 attendance row 는 이미 존재해야 함.
            # upcoming/late/no_show 상태에서 clock-in 시 update.
            target = await attendance_repository.get_by_schedule_id(db, schedule.id)
            if target is not None:
                before_status = target.status
                before_clock_in = target.clock_in
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
                if early_clock_in_override and early_override_preapproved:
                    # 매니저가 그 자리에서 승인한 조기 출근 — 라벨은 남기되 확인은 끝난 것.
                    target.early_clock_in_confirmed_by = manager_user_id
                    target.early_clock_in_confirmed_at = now
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
                        "early_clock_in_confirmed_by": (
                            manager_user_id
                            if early_clock_in_override and early_override_preapproved
                            else None
                        ),
                        "early_clock_in_confirmed_at": (
                            now
                            if early_clock_in_override and early_override_preapproved
                            else None
                        ),
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
            before_status = attendance.status
            touched_break = new_break
            break_before = (None, None, None)
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
            elapsed_minutes = minutes_between(open_break.started_at, now)
            policy_error = validate_break_end(open_break.break_type, elapsed_minutes, reason)
            if policy_error is not None:
                raise BadRequestError(policy_error)

            before_status = attendance.status
            touched_break = open_break
            break_before = (open_break.started_at, open_break.ended_at, open_break.break_type)
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
                if _sch is not None and (_sch.end_at is not None or _sch.end_time is not None):
                    _, sched_end_dt = resolve_schedule_instants(
                        start_at=_sch.start_at, end_at=_sch.end_at, work_date=_sch.work_date,
                        start_time=_sch.start_time, end_time=_sch.end_time, tz_name=store_tz,
                    )
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
                    if now.replace(second=0, microsecond=0) < sched_end_dt - _td2(minutes=_early_thresh):
                        is_early = True
            if not skip_early_guards and is_early and not (reason and reason.strip()):
                raise BadRequestError(
                    "Early clock-out requires a reason. Please provide one."
                )

            before_status = attendance.status
            before_clock_out = attendance.clock_out
            # 진행중 break 가 있으면 먼저 종료 처리
            if attendance.status == "on_break":
                open_break = await self._get_open_break(db, attendance.id)
                if open_break is not None:
                    touched_break = open_break
                    break_before = (
                        open_break.started_at, open_break.ended_at, open_break.break_type,
                    )
                    open_break.ended_at = now
                    open_break.duration_minutes = minutes_between(
                        open_break.started_at, now
                    )
                    attendance.break_end = now
            attendance.clock_out = now
            attendance.clock_out_timezone = store_tz
            attendance.status = "clocked_out"
            if attendance.clock_in is not None:
                attendance.total_work_minutes = minutes_between(attendance.clock_in, now)
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
        # action(카드 태그) 은 매니저 대행이어도 실제 행위(clock_in 등) 를 그대로 쓴다.
        # 예전엔 대행이면 "modify" 로 뭉개서 무엇이 바뀐지 알 수 없었고 before 도 비었다.
        # 대행 여부는 actor(corrected_by) 로 이미 드러난다.
        from app.services import attendance_timeline as tl

        actor_id = manager_user_id if skip_pin_check else user.id
        group = tl.new_group()
        tl.record_status(
            db,
            attendance_id=attendance.id,
            group_id=group,
            action=action,
            before=before_status,
            after=attendance.status,
            reason=reason,
            by_user_id=actor_id,
        )
        if action == "clock_in":
            tl.record(
                db,
                attendance_id=attendance.id,
                group_id=group,
                action=action,
                field_name=tl.FIELD_CLOCK_IN,
                before=tl.dt_value(before_clock_in),
                after=tl.dt_value(attendance.clock_in),
                reason=reason,
                by_user_id=actor_id,
            )
        elif action == "clock_out":
            tl.record(
                db,
                attendance_id=attendance.id,
                group_id=group,
                action=action,
                field_name=tl.FIELD_CLOCK_OUT,
                before=tl.dt_value(before_clock_out),
                after=tl.dt_value(attendance.clock_out),
                reason=reason,
                by_user_id=actor_id,
            )
        if touched_break is not None:
            tl.record_break_snapshot(
                db,
                attendance_id=attendance.id,
                group_id=group,
                action=action,
                break_id=touched_break.id,
                before=break_before,
                after=(
                    touched_break.started_at,
                    touched_break.ended_at,
                    touched_break.break_type,
                ),
                reason=reason,
                by_user_id=actor_id,
            )

        await self.touch_last_seen(db, device)

        # 조기 출근 강행 알림 — 직원이 스스로 찍은 건만. 매니저 대행은 그 매니저가
        # 이미 알고 있으므로 알리지 않는다. 실패해도 출근 자체는 성립해야 한다.
        notify_early = (
            action == "clock_in"
            and early_clock_in_override
            and not early_override_preapproved
        )

        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        if notify_early:
            await self._notify_early_clock_in(
                db,
                attendance=attendance,
                device=device,
                user=user,
                store_tz=store_tz,
                scheduled_start=scheduled_start,
                reason=reason or "",
            )
        return attendance

    async def _notify_early_clock_in(
        self,
        db: AsyncSession,
        *,
        attendance: Attendance,
        device: AttendanceDevice,
        user: User,
        store_tz: str,
        scheduled_start: datetime | None,
        reason: str,
    ) -> None:
        """조기 출근 강행 in-app 알림 + email (best-effort).

        알림 실패가 출근 기록을 되돌리면 안 된다 — 전체를 삼킨다.
        """
        try:
            from zoneinfo import ZoneInfo as _Zone

            from app.services.alert_service import alert_service
            from app.utils.names import display_name

            if scheduled_start is None or attendance.clock_in is None:
                return
            minutes_early = max(
                0,
                int(
                    (
                        scheduled_start
                        - attendance.clock_in.replace(second=0, microsecond=0)
                    ).total_seconds()
                    / 60
                ),
            )
            staff_name = display_name(user)

            await alert_service.create_for_early_clock_in(
                db,
                attendance_id=attendance.id,
                organization_id=device.organization_id,
                store_id=attendance.store_id,
                staff_user_id=user.id,
                staff_name=staff_name,
                minutes_early=minutes_early,
            )
            await db.commit()

            # email — 사용자 선호 가드는 should_send_email 이 담당 (checklist 와 동일).
            import asyncio

            from app.models.organization import Store as _Store
            from app.models.permission import Permission, RolePermission
            from app.models.user import Role
            from app.utils.email import send_email
            from app.utils.email_templates import build_early_clock_in_email

            store_name = await db.scalar(
                select(_Store.name).where(_Store.id == attendance.store_id)
            )
            recipients = await db.execute(
                select(User.id, User.email)
                .join(Role, User.role_id == Role.id)
                .join(RolePermission, Role.id == RolePermission.role_id)
                .join(Permission, RolePermission.permission_id == Permission.id)
                .where(User.organization_id == device.organization_id)
                .where(User.is_active.is_(True))
                .where(Permission.code == "schedules:update")
                .where(User.id != user.id)
                .where(User.email.is_not(None))
                .distinct()
            )
            tz = _Zone(store_tz)
            subject, html = build_early_clock_in_email(
                staff_name=staff_name,
                store_name=store_name or "your store",
                minutes_early=minutes_early,
                scheduled_start_label=scheduled_start.astimezone(tz).strftime(
                    "%-I:%M %p"
                ),
                clock_in_label=attendance.clock_in.astimezone(tz).strftime("%-I:%M %p"),
                reason=reason or "(no reason provided)",
            )
            for uid, email in recipients.all():
                if not email:
                    continue
                if not await alert_service.should_send_email(
                    db, uid, "early_clock_in_override"
                ):
                    continue
                asyncio.create_task(send_email(to=email, subject=subject, html=html))
        except Exception:
            # 알림 실패는 출근 성립에 영향 없음
            pass

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
