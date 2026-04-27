"""Attendance Device 서비스 — 매장 공용 기기 등록 + PIN 기반 clock in/out.

Attendance Device service layer — Handles terminal registration, token
verification, store assignment, and PIN-based clock operations that a
shared store device performs on behalf of any staff member.
"""

from __future__ import annotations

import hashlib
import secrets
import string
from datetime import date, datetime, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.attendance import Attendance
from app.models.attendance_break import (
    VALID_BREAK_TYPES,
    BREAK_TYPE_PAID_SHORT,
    BREAK_TYPE_UNPAID_LONG,
    AttendanceBreak,
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


async def generate_unique_clockin_pin(db: AsyncSession, organization_id: UUID) -> str:
    """organization 단위 unique 한 6자리 숫자 PIN 생성.

    조직당 가능한 PIN 공간(100만)이 작아 운영상 안전. 50회 시도 후에도 실패하면 예외.
    """
    for _ in range(50):
        candidate = f"{secrets.randbelow(1_000_000):06d}"
        result = await db.execute(
            select(User.id).where(
                User.organization_id == organization_id,
                User.clockin_pin == candidate,
            )
        )
        if result.scalar_one_or_none() is None:
            return candidate
    raise RuntimeError("Failed to allocate unique clockin PIN")


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
        """평문 토큰 → 활성 기기 조회. revoked 는 반환 안 함."""
        token_hash = hash_token(token)
        result = await db.execute(
            select(AttendanceDevice).where(
                AttendanceDevice.token_hash == token_hash,
                AttendanceDevice.revoked_at.is_(None),
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
            # 같은 store 의 non-revoked 기기 수 (자기 자신 제외)
            count_stmt = (
                select(_func.count(AttendanceDevice.id))
                .where(
                    AttendanceDevice.store_id == store_id,
                    AttendanceDevice.revoked_at.is_(None),
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
        """해제 — revoked_at 기록. Row 는 감사용으로 유지."""
        if device.revoked_at is None:
            device.revoked_at = datetime.now(timezone.utc)
            await db.flush()

    async def list_for_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
        include_revoked: bool = False,
    ) -> list[AttendanceDevice]:
        stmt = select(AttendanceDevice).where(
            AttendanceDevice.organization_id == organization_id
        )
        if not include_revoked:
            stmt = stmt.where(AttendanceDevice.revoked_at.is_(None))
        stmt = stmt.order_by(AttendanceDevice.registered_at.desc())
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_admin(
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

    async def verify_user_pin(
        self, db: AsyncSession, user_id: UUID, pin: str, organization_id: UUID
    ) -> User:
        """user_id 로 유저를 조회 후 PIN 이 일치하는지 확인.

        기존 PIN → user 매핑 대신 user + PIN 검증 방식. 유저 없음/PIN 불일치
        모두 400 (device token 은 유효하므로 401 로 반환하지 않는다).
        """
        if not pin or len(pin) != 6 or not pin.isdigit():
            raise BadRequestError("PIN must be 6 digits")
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
    ) -> Attendance:
        """기기 + user_id + PIN 으로 clock in/out/break 처리.

        break 는 attendance_breaks 테이블에 행 단위로 기록. break-start 는
        break_type 필수 (paid_short | unpaid_long). 같은 attendance 에 여러 번
        휴식 가능하며 open 상태 (ended_at IS NULL) 는 1건만 허용.
        """
        if device.store_id is None:
            raise BadRequestError("Device has no store assigned")

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
            # 1) 이미 진행중인 shift(working/on_break/late) 가 있으면 거절.
            active = next((r for r in day_rows if r.status in ("working", "on_break", "late")), None)
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

            # 우선순위 1: 현재 window (start <= now <= end) 안에 있는 스케줄
            schedule = None
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

            status_val = "working"
            anomalies: list[str] | None = None
            if schedule.start_time is not None:
                scheduled_start = datetime.combine(today, schedule.start_time, tzinfo=tz)
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
                    "break_type required (paid_short or unpaid_long)"
                )
            open_break = await self._get_open_break(db, attendance.id)
            if open_break is not None:
                raise BadRequestError("A break is already in progress")
            new_break = AttendanceBreak(
                attendance_id=attendance.id,
                started_at=now,
                break_type=break_type,
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
            open_break.ended_at = now
            delta = now - open_break.started_at
            open_break.duration_minutes = max(0, int(delta.total_seconds() / 60))
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
            await db.flush()
            await db.refresh(attendance)
        else:
            raise BadRequestError(f"Invalid action: {action}")

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
        paid = sum(b.duration_minutes or 0 for b in breaks if b.break_type == BREAK_TYPE_PAID_SHORT)
        unpaid = sum(
            b.duration_minutes or 0 for b in breaks if b.break_type == BREAK_TYPE_UNPAID_LONG
        )
        current = next((b for b in breaks if b.ended_at is None), None)
        return {
            "paid_minutes": paid,
            "unpaid_minutes": unpaid,
            "current": current,
            "all": breaks,
        }


attendance_device_service = AttendanceDeviceService()
