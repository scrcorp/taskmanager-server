"""Attendance Device 관련 Pydantic 스키마.

Request/response schemas for attendance terminal endpoints and the
admin device management surface.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# ── 앱/기기 측 요청 ────────────────────────────────────────

class RegisterRequest(BaseModel):
    """POST /attendance/register — access code 로 기기 등록."""
    access_code: str = Field(..., min_length=4, max_length=32)
    fingerprint: str | None = None


class RegisterResponse(BaseModel):
    """register 응답 — 평문 token 을 여기서만 1회 반환."""
    token: str
    device_id: UUID
    device_name: str
    store_id: UUID | None


class DeviceMeResponse(BaseModel):
    """GET /attendance/me — 현재 기기 정보."""
    device_id: UUID
    device_name: str
    organization_id: UUID
    store_id: UUID | None
    store_name: str | None
    store_timezone: str | None = None   # IANA tz, e.g. "America/Los_Angeles"
    store_timezone_offset_minutes: int | None = None  # 현재 UTC 오프셋 (분, 예: PDT=-420)
    work_date: str | None = None         # store tz + day_start 기준 "YYYY-MM-DD"
    registered_at: datetime
    last_seen_at: datetime | None


class AssignStoreRequest(BaseModel):
    """PUT /attendance/store — 매장 선택/변경."""
    store_id: UUID


class ClockActionRequest(BaseModel):
    """POST /attendance/clock-in 등 공용 요청.

    `break_type` 은 break-start 요청에만 의미 있음.
    - 'paid_short' : 10분 유급 짧은 휴식
    - 'unpaid_long': 30분 무급 긴 휴식

    `user_id` 는 기기에서 PIN 입력과 함께 전달되는 유저 식별자.
    서버는 user_id 로 유저를 조회한 뒤 PIN 이 일치하는지 확인한다.
    (기존에 PIN → user 역매칭을 하던 방식을 user + PIN 검증으로 변경)
    """
    user_id: UUID
    pin: str = Field(..., min_length=6, max_length=6)
    break_type: str | None = None


class TodayStaffBreak(BaseModel):
    """현재 진행 중인 break 요약 (today-staff 응답용)."""
    started_at: datetime
    break_type: str


class TodayStaffRow(BaseModel):
    """today-staff 엔드포인트 1건 — 유저 + 해당 schedule + attendance.

    Split shift 지원: 같은 user 가 하루 여러 shift 를 가지면 여러 row 로 반환됨.
    각 row 는 schedule_id 로 구분된다.

    `*_display` 필드는 store 타임존 기준 HH:mm 문자열. 클라이언트는
    별도 타임존 변환 없이 그대로 표시.

    `status` 는 DB 의 attendance.status 를 기반으로 하되, "upcoming" 이면
    서버가 요청 시점과 late_buffer_minutes 를 고려해 "soon" / "late" 로
    격상된 값을 내려준다. 클라이언트는 별도 분류 로직 없이 그대로 렌더.
    """
    user_id: UUID
    user_name: str
    schedule_id: UUID | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    scheduled_start_display: str | None = None  # "HH:mm" (store tz)
    scheduled_end_display: str | None = None
    clock_in: datetime | None = None
    clock_out: datetime | None = None
    clock_in_display: str | None = None          # "HH:mm" (store tz)
    clock_out_display: str | None = None
    status: str  # upcoming | soon | working | on_break | late | clocked_out | no_show | cancelled
    current_break: TodayStaffBreak | None = None
    paid_break_minutes: int = 0
    unpaid_break_minutes: int = 0


class NoticeRow(BaseModel):
    """공지 요약 (notices 엔드포인트)."""
    id: UUID
    title: str
    body: str | None = None
    created_at: datetime


# ── Admin 측 ───────────────────────────────────────────────

class AdminDeviceResponse(BaseModel):
    """Admin 목록/상세 응답."""
    id: UUID
    organization_id: UUID
    store_id: UUID | None
    store_name: str | None
    device_name: str
    fingerprint: str | None
    registered_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None


class AdminDeviceRenameRequest(BaseModel):
    device_name: str = Field(..., min_length=1, max_length=100)


class AdminAccessCodeResponse(BaseModel):
    service_key: str
    code: str
    source: str
    rotated_at: datetime | None
    created_at: datetime


# ── Clockin PIN ────────────────────────────────────────────

class ClockinPinResponse(BaseModel):
    """개인 PIN 조회 응답 — 본인 또는 admin lookup."""
    user_id: UUID
    clockin_pin: str | None


class AttendanceStoreOption(BaseModel):
    """기기 입장에서 선택 가능한 매장 후보 최소 정보."""
    id: UUID
    name: str
