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
    - 'paid_10min' : 10분 유급 짧은 휴식 (구: 'paid_short' — dual-read 호환)
    - 'unpaid_meal': 무급 식사 휴식 (구: 'unpaid_long' — dual-read 호환)

    `user_id` 는 기기에서 PIN 입력과 함께 전달되는 유저 식별자.
    서버는 user_id 로 유저를 조회한 뒤 PIN 이 일치하는지 확인한다.
    (기존에 PIN → user 역매칭을 하던 방식을 user + PIN 검증으로 변경)
    """
    user_id: UUID
    pin: str = Field(..., min_length=6, max_length=6)
    break_type: str | None = None
    # Early clock-out 사유. clock-out 시점이 schedule end - threshold 이전이면 필수.
    # 그 외엔 무시.
    reason: str | None = None


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


class ClockinPinUpdateRequest(BaseModel):
    """PIN 수동 변경 요청. 4~6자리 숫자."""
    clockin_pin: str = Field(..., pattern=r"^\d{4,6}$")


class AttendanceStoreOption(BaseModel):
    """기기 입장에서 선택 가능한 매장 후보 최소 정보."""
    id: UUID
    name: str


# ── Identify-by-PIN (Phase 3) ─────────────────────────────────


class IdentifyByPinRequest(BaseModel):
    """PIN 단독 식별 요청 — 4~6자리 숫자."""
    pin: str = Field(..., pattern=r"^\d{4,6}$")


class IdentifyByPinCurrentBreak(BaseModel):
    """on_break 일 때 현재 진행 중인 break 정보 — kiosk 가 break info 박스 표시용."""
    break_type: str
    started_at: datetime


class IdentifyByPinResponse(BaseModel):
    """PIN 식별 응답 — 직원 clock 흐름 entry.

    today_status: 오늘 attendance 가 있으면 dashboard 와 동일한 effective status,
    스케줄 없으면 None. clock 가능 여부/UI 분기에 사용.
    current_break: on_break 상태일 때만 채워짐 (그 외 None).
    scheduled_end: 오늘 schedule 의 종료 시각 (UTC). 클럭아웃 시 early-checkout
                   threshold 비교용. 스케줄 없으면 None.
    """
    user_id: UUID
    user_name: str
    today_status: str | None
    current_break: IdentifyByPinCurrentBreak | None = None
    scheduled_end: datetime | None = None


# ── Kiosk 관리자 모드 ──────────────────────────────────────
# Settings 화면에서 SV/GM/Owner PIN 으로 진입. 짧은 in-memory 세션 토큰 발급.


class ManageManagerOption(BaseModel):
    """관리자 모드에 진입 가능한 매장 매니저 1명."""
    user_id: UUID
    full_name: str
    role_name: str
    role_priority: int


class ManageSessionRequest(BaseModel):
    """매니저 PIN 으로 manage session 발급. PIN 으로 user 식별 + 매니저 자격 검증."""
    pin: str = Field(..., min_length=4, max_length=6)


class ManageSessionResponse(BaseModel):
    """admin session 발급 결과."""
    manage_token: str
    manager_user_id: UUID
    manager_name: str
    expires_at: datetime


class ManageScheduleRow(BaseModel):
    """오늘 매장 스케줄 1건 (관리자 모드 리스트용)."""
    schedule_id: UUID
    user_id: UUID
    user_name: str
    work_role_id: UUID | None
    work_role_name: str | None
    shift_name: str | None = None
    position_name: str | None
    start_time: str | None  # "HH:mm" (store tz)
    end_time: str | None
    status: str
    attendance_id: UUID | None
    attendance_status: str | None
    clock_in_display: str | None = None   # "HH:mm" (store tz)
    clock_out_display: str | None = None  # "HH:mm" (store tz)


class AdminStatusChangeRequest(BaseModel):
    """관리자가 attendance status 를 직접 변경할 때.

    상태별로 함께 반영해야 할 시각이 다르다.
      - working / late: clock_in 시각 필수 (없으면 기존 유지하거나 NULL→지금)
      - clocked_out:    clock_in 유지 + clock_out 시각 필수
      - upcoming:       clock_in/out 모두 클리어 (cancel_clock_in 동일 효과)
      - no_show:        clock_in/out 모두 클리어 (출근 없음 확정)
      - on_break / soon: 시간 변경 없이 status 만 토글
    reason 은 선택. 매니저가 나중에 적어도 되므로 빈 값 허용 → 서버가 fallback 적용.
    """
    user_id: UUID
    status: str
    reason: str | None = None
    # 선택적 시간 보정 ("HH:mm" store tz). 현재 work_date 의 store tz datetime 으로 합성.
    clock_in_hhmm: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    clock_out_hhmm: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")


class ManageAssignableUser(BaseModel):
    """오늘 새 스케줄을 배정할 수 있는 매장 직원."""
    user_id: UUID
    full_name: str
    role_name: str


class ManageWorkRoleOption(BaseModel):
    """매장 work role 옵션 (스케줄 생성/수정 select).

    shift_name + position_name 조합으로 표시. work role 자체 name 은 비어있는
    경우가 흔해서 클라이언트에서 "{shift} · {position}" 형태로 합성한다.
    """
    work_role_id: UUID
    name: str | None
    shift_name: str | None
    position_name: str | None
    default_start_time: str | None
    default_end_time: str | None


class ManageScheduleCreateRequest(BaseModel):
    """관리자가 오늘 스케줄을 새로 만들 때."""
    user_id: UUID
    work_role_id: UUID | None = None
    start_time: str = Field(..., pattern=r"^\d{2}:\d{2}$")  # "HH:mm"
    end_time: str = Field(..., pattern=r"^\d{2}:\d{2}$")


class ManageScheduleUpdateRequest(BaseModel):
    """관리자가 오늘 스케줄 시간/배정을 수정할 때."""
    user_id: UUID | None = None
    work_role_id: UUID | None = None
    start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    end_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")


class AdminClockActionRequest(BaseModel):
    """관리자가 임의 사용자 attendance 를 override 할 때.

    actions: "clock_in" | "clock_out" | "break_start" | "break_end" | "cancel_clock_in"
    "cancel_clock_in" 은 잘못 찍힌 출근을 초기화 (attendance status → upcoming).
    """
    user_id: UUID
    action: str
    break_type: str | None = None
    reason: str | None = None

