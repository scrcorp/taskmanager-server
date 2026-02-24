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

from sqlalchemy import Boolean, Date, DateTime, Integer, String, Text, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

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

    Status flow: clocked_in -> on_break -> clocked_in -> clocked_out

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
        uq_attendance_user_date: 동일 사용자+날짜 중복 불가
            (One attendance record per user per day)
    """

    __tablename__ = "attendances"

    # 근태 고유 식별자 — Attendance unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope for multi-tenant data isolation
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # 매장 FK — Store where user clocked in via QR scan
    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    # 사용자 FK — User who recorded attendance
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
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
    # 상태 — Status: "clocked_in" → "on_break" → "clocked_in" → "clocked_out"
    status: Mapped[str] = mapped_column(String(20), default="clocked_in")
    # 총 근무 시간(분) — Auto-calculated on clock_out: (clock_out - clock_in) in minutes
    total_work_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 총 휴식 시간(분) — Auto-calculated: (break_end - break_start) in minutes
    total_break_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 메모 — Optional note
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "work_date", name="uq_attendance_user_date"),
    )


class AttendanceCorrection(Base):
    """근태 수정 이력 모델 — 관리자가 근태 기록을 수정한 이력.

    Attendance correction audit trail model — Records when an admin
    corrects an attendance field (clock_in, clock_out, etc.).

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        attendance_id: 근태 기록 FK (Target attendance record)
        field_name: 수정된 필드 이름 (Which field was corrected)
        original_value: 수정 전 값 (Original value before correction)
        corrected_value: 수정 후 값 (New corrected value)
        reason: 수정 사유 (Reason for correction)
        corrected_by: 수정자 FK (Admin who made the correction)
        created_at: 수정 일시 UTC (Correction timestamp)
    """

    __tablename__ = "attendance_corrections"

    # 수정 이력 고유 식별자 — Correction unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 근태 기록 FK — Target attendance record being corrected
    attendance_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("attendances.id", ondelete="CASCADE"), nullable=False)
    # 수정된 필드 이름 — Field that was corrected (e.g. "clock_in", "clock_out")
    field_name: Mapped[str] = mapped_column(String(50), nullable=False)
    # 수정 전 값 — Original value before correction (ISO datetime string or null)
    original_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 수정 후 값 — New corrected value (ISO datetime string)
    corrected_value: Mapped[str] = mapped_column(Text, nullable=False)
    # 수정 사유 — Reason for the correction
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # 수정자 FK — Admin/manager who made the correction
    corrected_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=False)
    # 수정 일시 — Correction timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
