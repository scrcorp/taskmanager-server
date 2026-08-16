"""근태 관리 관련 SQLAlchemy ORM 모델 정의.

Attendance management SQLAlchemy ORM model definitions.
Includes QR codes for store check-in, daily attendance records,
and attendance correction audit trail.

Tables:
    - qr_codes: 매장별 QR 코드 (Store QR codes for attendance scanning)
    - attendances: 근태 기록 (Daily attendance records per user)
    - attendance_corrections: 근태 수정 이력 (Attendance correction audit trail)
"""

import uuid
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Index, Integer, String, Text, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.client_surface import current_channel
from app.database import Base


class QRCode(Base):
    """매장별 QR 코드 모델 — 출퇴근 스캔용.

    Store QR code model — Used for attendance clock-in/out scanning.
    Each store has one active QR code at a time. When a new QR is generated,
    the previous one is deactivated.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        store_id: 매장 FK (Store where this QR is used)
        code: 고유 QR 코드 문자열 (Unique random code for QR generation)
        is_active: 활성 상태 (Whether this QR code is currently active)
        created_by: 생성자 FK (User who generated this QR code)
        created_at: 생성 일시 UTC (Creation timestamp)
        expires_at: 만료 일시, 선택 (Optional expiration timestamp)
    """

    __tablename__ = "qr_codes"

    # QR 코드 고유 식별자 — QR code unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 매장 FK — Store where this QR code is used for scanning
    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    # 고유 QR 코드 문자열 — Random unique 32-char hex code for QR generation
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # 활성 상태 — Whether this QR code is currently active (one active per store)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 생성자 FK — User who generated this QR code (nullable for system-generated)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 만료 일시 — Optional expiration timestamp (null = no expiration)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Attendance(Base):
    """근태 기록 모델 — 일별 사용자 출퇴근 기록.

    Attendance record model — Daily user clock-in/out record.
    One record per user per work date. Tracks clock-in, break, and clock-out times
    with timezone information and auto-calculated work/break durations.

    Status flow: upcoming → working → on_break → working → clocked_out
                 (late, no_show as alternates; cancelled for rejected/cancelled/deleted schedules)

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        organization_id: 소속 조직 FK (Organization scope for multi-tenant isolation)
        store_id: 매장 FK (Store where user clocked in)
        user_id: 사용자 FK (User who clocked in)
        work_date: 근무 날짜 (Date of attendance)
        clock_in: 출근 시각 (Clock-in timestamp)
        clock_in_timezone: 출근 시 타임존 (Timezone at clock-in)
        break_start: 휴식 시작 시각 (Break start timestamp)
        break_end: 휴식 종료 시각 (Break end timestamp)
        clock_out: 퇴근 시각 (Clock-out timestamp)
        clock_out_timezone: 퇴근 시 타임존 (Timezone at clock-out)
        status: 상태 (Status: clocked_in, on_break, clocked_out)
        total_work_minutes: 총 근무 시간(분) (Auto-calculated total work minutes)
        total_break_minutes: 총 휴식 시간(분) (Auto-calculated total break minutes)
        note: 메모 (Optional note)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Constraints:
        uq_attendance_schedule (partial): schedule_id IS NOT NULL 일 때 schedule당 1개.
        uq_attendance_walkin (partial): schedule_id IS NULL 일 때 user+work_date 1개.
    """

    __tablename__ = "attendances"

    # 근태 고유 식별자 — Attendance unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope for multi-tenant data isolation
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # 매장 FK — Store where user clocked in via QR scan (SET NULL: 매장 삭제 시 null)
    store_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True)
    # 사용자 FK — User who recorded attendance (SET NULL: 사용자 삭제 시 null)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 연결된 스케줄 FK — Linked schedule (nullable: 스케줄 없이 출근한 edge case 허용)
    schedule_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True)
    # 근무 날짜 — Date of attendance (date only, no time)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 출근 시각 — Clock-in timestamp with timezone
    clock_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 출근 타임존 — IANA timezone at clock-in (e.g. "America/Los_Angeles")
    clock_in_timezone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 휴식 시작 시각 — Break start timestamp
    break_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 휴식 종료 시각 — Break end timestamp
    break_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 퇴근 시각 — Clock-out timestamp with timezone
    clock_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 퇴근 타임존 — IANA timezone at clock-out
    clock_out_timezone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 상태 — Status: upcoming/working/on_break/late/clocked_out/no_show/cancelled
    status: Mapped[str] = mapped_column(String(20), default="upcoming")
    # 이상 항목 — anomaly flags: ['late', 'early_leave', 'no_break', 'overtime', 'no_show']
    anomalies: Mapped[list[str] | None] = mapped_column(ARRAY(String(30)), nullable=True)
    # 총 근무 시간(분) — Auto-calculated on clock_out: (clock_out - clock_in) in minutes
    total_work_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 총 휴식 시간(분) — Auto-calculated: (break_end - break_start) in minutes
    total_break_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 메모 — Optional note
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 자동퇴근 확인자 FK — Manager/SV who confirmed the auto clock-out (payroll close gate ①)
    auto_clock_out_confirmed_by: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 자동퇴근 확인 일시 — When the auto clock-out was confirmed (UTC)
    auto_clock_out_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 조기 출근 강행 확인자 FK — Manager/SV who reviewed the early clock-in override
    early_clock_in_confirmed_by: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 조기 출근 강행 확인 일시 — payroll 마감 게이트의 판정 근거 (UTC)
    early_clock_in_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 조기 출근 **요청자**(= "누가 일찍 오라고 했나") FK — D9.
    # 표시용 문자열("Asked to come in early (John Kim)")은 attendance_corrections.reason 에
    # 남고, 식별은 여기서 한다. 문자열만 남기면 동명이인 구분·개명 후 추적·요청자별 집계가
    # 전부 불가능하고 **나중에 소급해서 채울 수 없다.**
    # "직접 입력(Someone else)" 은 명단 밖 사람이라 id 자체가 없으므로 NULL 이 정상이다
    # (구버전 HTMA 도 이 값을 보내지 않는다 — 비어 있는 게 정상인 컬럼).
    early_clock_in_requested_by: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        # schedule에 묶인 attendance는 schedule 당 1개 (partial unique)
        Index(
            "uq_attendance_schedule",
            "schedule_id",
            unique=True,
            postgresql_where=(schedule_id.is_not(None)),
        ),
        # walk-in (schedule_id NULL) 은 user + work_date 조합으로 1개
        Index(
            "uq_attendance_walkin",
            "user_id",
            "work_date",
            unique=True,
            postgresql_where=(schedule_id.is_(None)),
        ),
        Index("ix_attendances_schedule_id", "schedule_id"),
    )


class AttendanceCorrection(Base):
    """근태 타임라인 이력 모델 — 근태 기록에 일어난 모든 전이를 남긴다.

    Attendance timeline / audit trail model. 직원의 실제 행동(출근·휴식)과
    관리자의 정정을 같은 테이블에 쌓되, **모든 행이 before → after 전이를
    빠짐없이 표현**한다. "이전 값이 없다"는 상태는 존재하지 않는다 —
    비어 있었다면 그 사실을 `(none)` / `(empty)` 센티널로 명시한다.

    한 사용자 액션이 여러 축을 바꾸면(예: 출근 = status 전이 + clock_in 값 전이)
    행을 나눠 쓰고 `group_id` 로 묶는다. 카드 제목은 `action`, 각 줄의 항목명은
    `field_name` 이다.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        attendance_id: 근태 기록 FK (Target attendance record)
        group_id: 액션 그룹 — 한 사용자 액션이 만든 행들을 묶는다
        action: 카드 태그 — clock_in / break_start / modify 등 (무엇을 했나)
        field_name: 전이 대상 항목 — status / clock_in / break_time 등 (무엇이 바뀌었나)
        target_type: 전이 대상 엔터티 종류 — "attendance" | "break"
        target_id: 하위 엔터티 식별자 (break 세션 id 등). attendance 자신이면 NULL
        original_value: 전이 전 값 (Value before). 신규 행은 항상 채워진다
        corrected_value: 전이 후 값 (Value after)
        reason: 사유 (Reason)
        corrected_by: 행위자 FK (Actor). NULL = system (cron)
        channel: 기록 경로 — 어느 클라이언트 표면에서 이 전이가 만들어졌나
        created_at: 발생 일시 UTC (Timestamp)
    """

    __tablename__ = "attendance_corrections"

    # 수정 이력 고유 식별자 — Correction unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 근태 기록 FK — Target attendance record being corrected
    attendance_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("attendances.id", ondelete="CASCADE"), nullable=False)
    # 액션 그룹 — 한 사용자 액션이 만든 행들을 묶는 식별자.
    # NULL = 이 컬럼 도입 이전 레거시 행 (콘솔이 시간 근접 휴리스틱으로 fallback).
    group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # 카드 태그 — 무엇을 했나 (clock_in / clock_out / break_start / break_end /
    # modify / no_show / cancel / reopen / auto_clock_out / break_added 등).
    # NULL = 레거시 행 → 콘솔은 field_name 으로 fallback.
    action: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # 전이 대상 항목 이름 — 무엇이 바뀌었나 ("status", "clock_in", "break_time" 등)
    field_name: Mapped[str] = mapped_column(String(50), nullable=False)
    # 전이 대상 엔터티 종류 — "attendance" | "break". NULL = 레거시 행(= attendance)
    target_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 하위 엔터티 식별자 — break 세션 id 등. attendance 본체 전이면 NULL.
    # FK 를 걸지 않는다: 세션이 삭제돼도 "삭제했다"는 이력은 남아야 한다.
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # 전이 전 값 — Value before. 신규 행은 항상 채운다 (비어 있었으면 "(none)").
    # nullable 유지 = 이 정책 도입 이전 레거시 행 때문 (백필하지 않음).
    original_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 수정 후 값 — New corrected value (ISO datetime string)
    corrected_value: Mapped[str] = mapped_column(Text, nullable=False)
    # 수정 사유 — Reason for the correction
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # 수정자 FK — Admin/manager who made the correction
    # nullable — system actor (cron auto clock-out) 는 user_id 없이 기록.
    corrected_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 채널 — 어느 경로로 이 전이가 기록됐나 (app/core/client_surface.py 상수):
    # console / console_compact / htma / staff_app / backoffice / system / api.
    # NULL = 채널 도입 전 레거시 행 (백필하지 않음).
    # Python-side default 로도 채운다 — timeline 헬퍼를 거치지 않는 생성 경로
    # (attendance_repository.create_correction 등)에서도 누락되지 않게.
    # 요청 컨텍스트 밖(cron)이면 current_channel() 이 "system" 을 반환한다.
    # 주의: zero-arg lambda 유지 — current_channel 을 직접 넘기면 SQLAlchemy 가
    # default 파라미터 자리에 ExecutionContext 를 주입한다.
    channel: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default=lambda: current_channel()
    )
    # 수정 일시 — Correction timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
