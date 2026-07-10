"""스케줄 시스템 Pydantic 스키마.

Schedule system Pydantic schemas for work roles, break rules, periods, requests, and entries.
"""

import re
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

# 스케줄 시간은 30분 grid(:00/:30)만 허용. 어긋나면 reject (반올림하지 않음).
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
SCHEDULE_STEP_MINUTES = 30


def validate_30min_grid(value: str | None) -> str | None:
    """"HH:MM" 가 30분 단위(:00/:30)인지 검증. None/"" 은 통과 (optional 필드)."""
    if value is None or value == "":
        return value
    m = _HHMM_RE.match(value)
    if not m:
        raise ValueError("Time must be in HH:MM format.")
    if int(m.group(2)) % SCHEDULE_STEP_MINUTES != 0:
        raise ValueError("Time must be on the hour or half-hour (:00 or :30).")
    return value


# ─── Work Role ───────────────────────────────────────


class WorkRoleCreate(BaseModel):
    shift_id: str
    position_id: str
    name: str | None = None
    default_start_time: str | None = None  # "HH:MM"
    default_end_time: str | None = None
    break_start_time: str | None = None
    break_end_time: str | None = None
    headcount: dict | None = None  # {"all": 1, "sun": 1, "mon": 1, ...}
    use_per_day_headcount: bool = False
    default_checklist_id: str | None = None
    is_active: bool = True
    sort_order: int = 0


class WorkRoleUpdate(BaseModel):
    name: str | None = None
    default_start_time: str | None = None
    default_end_time: str | None = None
    break_start_time: str | None = None
    break_end_time: str | None = None
    headcount: dict | None = None  # {"all": 1, "sun": 1, "mon": 1, ...}
    use_per_day_headcount: bool | None = None
    default_checklist_id: str | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class WorkRoleResponse(BaseModel):
    id: str
    store_id: str
    shift_id: str
    shift_name: str | None = None
    position_id: str
    position_name: str | None = None
    name: str | None
    default_start_time: str | None
    default_end_time: str | None
    break_start_time: str | None
    break_end_time: str | None
    headcount: dict  # {"all": 1, "sun": 1, "mon": 1, ...}
    use_per_day_headcount: bool
    default_checklist_id: str | None
    is_active: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class WorkRoleReorderItem(BaseModel):
    id: str
    sort_order: int


class WorkRoleReorderRequest(BaseModel):
    items: list[WorkRoleReorderItem]


# ─── Break Rule ──────────────────────────────────────


class BreakRuleUpsert(BaseModel):
    max_continuous_minutes: int = 240
    break_duration_minutes: int = 30
    max_daily_work_minutes: int = 480
    work_hour_calc_basis: str = "per_store"


class BreakRuleResponse(BaseModel):
    id: str
    store_id: str
    max_continuous_minutes: int
    break_duration_minutes: int
    max_daily_work_minutes: int
    work_hour_calc_basis: str
    created_at: datetime
    updated_at: datetime


# ─── Schedule Request Template ───────────────────────


class RequestTemplateItemCreate(BaseModel):
    day_of_week: int  # 0=Sun, 6=Sat
    work_role_id: str
    preferred_start_time: str | None = None
    preferred_end_time: str | None = None


class RequestTemplateCreate(BaseModel):
    store_id: str | None = None
    name: str
    is_default: bool = False
    items: list[RequestTemplateItemCreate] = []


class RequestTemplateUpdate(BaseModel):
    name: str | None = None
    is_default: bool | None = None
    items: list[RequestTemplateItemCreate] | None = None


class RequestTemplateItemResponse(BaseModel):
    id: str
    template_id: str
    day_of_week: int
    work_role_id: str
    work_role_name: str | None = None
    store_name: str | None = None
    preferred_start_time: str | None
    preferred_end_time: str | None


class RequestTemplateResponse(BaseModel):
    id: str
    user_id: str
    store_id: str | None = None
    name: str
    is_default: bool
    items: list[RequestTemplateItemResponse]
    created_at: datetime
    updated_at: datetime


# ─── Schedule Request ────────────────────────────────


class ScheduleRequestCreate(BaseModel):
    store_id: str
    work_role_id: str | None = None
    work_date: date
    preferred_start_time: str | None = None
    preferred_end_time: str | None = None
    break_start_time: str | None = None
    break_end_time: str | None = None
    note: str | None = None


class ScheduleRequestStatusUpdate(BaseModel):
    status: str  # accepted/modified/rejected


class ScheduleRequestUpdate(BaseModel):
    store_id: str | None = None
    work_role_id: str | None = None
    work_date: date | None = None
    preferred_start_time: str | None = None
    preferred_end_time: str | None = None
    break_start_time: str | None = None
    break_end_time: str | None = None
    note: str | None = None


class ScheduleRequestResponse(BaseModel):
    id: str
    user_id: str
    user_name: str | None = None
    store_id: str
    store_name: str | None = None
    work_role_id: str | None
    work_role_name: str | None = None
    work_date: date
    preferred_start_time: str | None
    preferred_end_time: str | None
    break_start_time: str | None = None
    break_end_time: str | None = None
    note: str | None
    status: str
    submitted_at: datetime
    created_at: datetime
    original_preferred_start_time: str | None = None
    original_preferred_end_time: str | None = None
    original_work_role_id: str | None = None
    original_user_id: str | None = None
    original_user_name: str | None = None
    original_work_date: date | None = None
    created_by: str | None = None
    rejection_reason: str | None = None
    hourly_rate: float | None = 0  # 신청 시급 (Resolved). SV/Staff에는 redact되어 None.


class ScheduleRequestFromTemplate(BaseModel):
    store_id: str
    date_from: date
    date_to: date
    template_id: str
    on_conflict: str = "skip"  # "skip" | "replace"


class ScheduleRequestCopyLastPeriod(BaseModel):
    store_id: str
    date_from: date
    date_to: date
    on_conflict: str = "skip"  # "skip" | "replace"


class ScheduleRequestSkippedItem(BaseModel):
    work_date: date
    work_role_id: str | None = None
    work_role_name: str | None = None
    reason: str


class ScheduleRequestFromTemplateResult(BaseModel):
    created: list[ScheduleRequestResponse] = []
    skipped: list[ScheduleRequestSkippedItem] = []
    replaced: list[ScheduleRequestResponse] = []


class ScheduleRequestBatchItem(BaseModel):
    """배치 제출 - 신규 생성 항목."""
    store_id: str
    work_date: date
    work_role_id: str | None = None
    preferred_start_time: str | None = None  # "HH:MM"
    preferred_end_time: str | None = None
    break_start_time: str | None = None
    break_end_time: str | None = None
    note: str | None = None


class ScheduleRequestBatchUpdate(BaseModel):
    """배치 제출 - 기존 수정 항목."""
    id: str
    store_id: str | None = None
    work_role_id: str | None = None
    work_date: date | None = None
    preferred_start_time: str | None = None
    preferred_end_time: str | None = None
    break_start_time: str | None = None
    break_end_time: str | None = None
    note: str | None = None


class ScheduleRequestBatchSubmit(BaseModel):
    """배치 제출 요청 — 생성/수정/삭제를 한번에."""
    creates: list[ScheduleRequestBatchItem] = []
    updates: list[ScheduleRequestBatchUpdate] = []
    deletes: list[str] = []  # request UUIDs


class ScheduleRequestBatchResult(BaseModel):
    """배치 제출 결과."""
    created: list[ScheduleRequestResponse] = []
    updated: list[ScheduleRequestResponse] = []
    deleted_count: int = 0
    errors: list[str] = []


class ScheduleRequestAdminCreate(BaseModel):
    """Admin creates a request on behalf (not visible to staff until confirm)."""
    store_id: str
    user_id: str
    work_role_id: str | None = None
    work_date: date
    preferred_start_time: str | None = None  # "HH:MM"
    preferred_end_time: str | None = None
    break_start_time: str | None = None
    break_end_time: str | None = None
    note: str | None = None
    hourly_rate: float | None = None  # 시급 override (optional — auto-calculated if not provided)


class ScheduleRequestAdminUpdate(BaseModel):
    """Admin modifies a request — changes time/role/user/date. Auto-tracks originals."""
    user_id: str | None = None
    work_role_id: str | None = None
    work_date: date | None = None
    preferred_start_time: str | None = None
    preferred_end_time: str | None = None
    break_start_time: str | None = None
    break_end_time: str | None = None
    note: str | None = None
    rejection_reason: str | None = None


class ScheduleConfirmRequest(BaseModel):
    """Bulk confirm requests for a date range → create schedule entries + work assignments."""
    store_id: str
    date_from: date
    date_to: date


class ScheduleConfirmResult(BaseModel):
    entries_created: int
    requests_confirmed: int
    requests_rejected: int
    errors: list[str] = []


class ScheduleConfirmPreviewFail(BaseModel):
    request_id: str
    user_name: str | None = None
    work_date: date
    reason: str


class ScheduleConfirmPreview(BaseModel):
    """Confirm dry-run 결과 — DB 변경 없이 예측만 반환."""
    will_confirm: int
    will_skip_rejected: int
    will_fail: list[ScheduleConfirmPreviewFail] = []


# ─── Schedule (확정 스케줄) ──────────────────────────


class ScheduleCreate(BaseModel):
    request_id: str | None = None
    user_id: str
    store_id: str
    work_role_id: str | None = None
    # 전환기(Wave 1): 구(舊) 필드(work_date + HH:MM)와 신(新) 필드(operating_day + ISO datetime) 둘 다 허용.
    # 서비스가 정규화. 신 필드가 우선. Wave 3에서 구 필드 제거.
    work_date: date | None = None  # 구: 영업일(now optional)
    start_time: str | None = None  # 구: "HH:MM"
    end_time: str | None = None
    break_start_time: str | None = None
    break_end_time: str | None = None
    # 신: operating_day(영업일 라벨) + start_at/end_at(벽시계 ISO "YYYY-MM-DDTHH:MM")
    operating_day: date | None = None
    start_at: str | None = None
    end_at: str | None = None
    break_start_at: str | None = None
    break_end_at: str | None = None
    note: str | None = None
    hourly_rate: float | None = Field(default=None, ge=0)  # 시급 override (optional, non-negative)
    status: str = "confirmed"  # "requested" for app submissions, "confirmed" for direct admin creation
    force: bool = False  # Override warnings

    _validate_times = field_validator(
        "start_time", "end_time", "break_start_time", "break_end_time"
    )(validate_30min_grid)


class ScheduleUpdate(BaseModel):
    user_id: str | None = None
    work_role_id: str | None = None
    work_date: date | None = None
    start_time: str | None = None
    end_time: str | None = None
    break_start_time: str | None = None
    break_end_time: str | None = None
    # 신: 전환기 datetime 필드 (create와 동일 정규화)
    operating_day: date | None = None
    start_at: str | None = None
    end_at: str | None = None
    break_start_at: str | None = None
    break_end_at: str | None = None
    note: str | None = None
    hourly_rate: float | None = Field(default=None, ge=0)  # 시급 override (optional, non-negative)
    force: bool = False
    reset_checklist: bool | None = None
    # user_id 변경 시 기존 체크리스트 처리:
    # None  = 충돌(in_progress/completed) 시 에러 반환 (프론트가 선택 후 재요청)
    # True  = 체크리스트 초기화
    # False = 진행 상태 그대로 유지

    _validate_times = field_validator(
        "start_time", "end_time", "break_start_time", "break_end_time"
    )(validate_30min_grid)


class ScheduleResponse(BaseModel):
    id: str
    organization_id: str
    request_id: str | None
    user_id: str
    user_name: str | None = None
    user_department: str | None = None  # 배정 직원의 FOH/BOH 분류 (스케줄 탭 필터용, None=미지정)
    store_id: str
    store_name: str | None = None
    work_role_id: str | None
    work_role_name: str | None = None
    # Snapshot — preserved at creation time, immune to later renames
    work_role_name_snapshot: str | None = None
    position_snapshot: str | None = None
    work_date: date
    start_time: str | None
    end_time: str | None
    break_start_time: str | None
    break_end_time: str | None
    # 신: 전환기 datetime 인코딩 (구 필드와 동시 노출). Wave 3에서 구 필드 제거.
    operating_day: date | None = None
    start_at: str | None = None  # "YYYY-MM-DDTHH:MM" 벽시계
    end_at: str | None = None
    break_start_at: str | None = None
    break_end_at: str | None = None
    net_work_minutes: int
    status: str
    created_by: str | None
    approved_by: str | None
    confirmed_at: datetime | None = None
    note: str | None
    hourly_rate: float | None = 0  # 스냅샷 시급 (저장 시점). NULL은 override 없음. SV/Staff는 redact.
    effective_rate: float | None = None  # 상속 체인(user → store → org)으로 계산된 실효 시급. redact 시 None.
    effective_rate_source: str | None = None  # "schedule" | "user" | "store" | "org" | None
    submitted_at: datetime | None = None
    is_modified: bool = False
    rejected_by: str | None = None
    rejected_at: datetime | None = None
    rejection_reason: str | None = None
    cancelled_by: str | None = None
    cancelled_at: datetime | None = None
    cancellation_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class ScheduleConfirm(BaseModel):
    """Confirm a requested schedule — changes status from requested to confirmed."""
    pass


class ScheduleReject(BaseModel):
    """Reject a requested schedule. Reason optional (nullable)."""
    rejection_reason: str | None = None


class ScheduleCancel(BaseModel):
    """Cancel a confirmed schedule (GM+ only). Reason optional (nullable)."""
    cancellation_reason: str | None = None


class ScheduleSwitch(BaseModel):
    """Switch two confirmed schedules' assigned users (GM+ only)."""
    other_schedule_id: str
    reason: str | None = None
    reset_checklists: bool | None = None
    force: bool = False  # 겹침 경고 무시
    # None  = 충돌(in_progress/completed) 시 에러 반환 (프론트가 선택 후 재요청)
    # True  = 양쪽 체크리스트 초기화
    # False = 진행 상태 그대로 유지

# backward compat alias
ScheduleSwap = ScheduleSwitch


class ScheduleAssignChecklist(BaseModel):
    """단일 스케줄에 체크리스트 템플릿 수동 부여."""
    template_id: str


class ScheduleAssignChecklistResult(BaseModel):
    instance_id: str
    template_id: str
    schedule_id: str


class ScheduleAuditLogResponse(BaseModel):
    id: str
    schedule_id: str
    event_type: str
    actor_id: str | None = None
    actor_name: str | None = None
    actor_role: str | None = None
    timestamp: datetime
    description: str | None = None
    reason: str | None = None
    diff: dict | None = None


class ScheduleHistoryItem(BaseModel):
    """집계 history 응답 — audit log + schedule snapshot 일부."""
    id: str
    schedule_id: str
    event_type: str
    actor_id: str | None = None
    actor_name: str | None = None
    actor_role: str | None = None
    timestamp: datetime
    description: str | None = None
    reason: str | None = None
    diff: dict | None = None
    # Schedule snapshot
    work_date: date
    start_time: str | None = None
    end_time: str | None = None
    user_id: str
    user_name: str | None = None
    store_id: str
    store_name: str | None = None
    schedule_status: str
    work_role_name: str | None = None


class ScheduleHistoryListResponse(BaseModel):
    items: list[ScheduleHistoryItem]
    total: int
    page: int
    per_page: int


class ScheduleBulkConfirm(BaseModel):
    """Bulk confirm all requested schedules in a date range."""
    store_id: str
    date_from: date
    date_to: date


class ScheduleBulkConfirmResult(BaseModel):
    confirmed: int = 0
    skipped: int = 0
    errors: list[str] = []


class ScheduleBulkCreate(BaseModel):
    entries: list[ScheduleCreate]
    skip_on_conflict: bool = False


class ScheduleBulkResult(BaseModel):
    created: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = []
    items: list["ScheduleResponse"] = []


class ScheduleValidation(BaseModel):
    valid: bool
    warnings: list[str] = []
    errors: list[str] = []


class FinalizeResult(BaseModel):
    created: int
    failed: int
    errors: list[str] = []


class BulkAssignChecklistRequest(BaseModel):
    """스케줄 일괄 체크리스트 할당/교체/제거 요청.

    Bulk checklist assign/replace/remove request for schedules.
    - checklist_template_id provided: create or replace cl_instance for each schedule
    - checklist_template_id is null: remove existing cl_instances for each schedule
    """

    schedule_ids: list[str]
    checklist_template_id: str | None = None


class BulkAssignChecklistResult(BaseModel):
    """스케줄 일괄 체크리스트 할당 결과.

    Result of bulk checklist assign/replace/remove.
    """

    assigned: int = 0
    removed: int = 0
    skipped: int = 0
    errors: list[str] = []


# ─── Bulk Preview ────────────────────────────────────


class BulkPreviewEntry(BaseModel):
    """벌크 preview 요청의 단일 항목 — ScheduleCreate 슬림 버전."""
    user_id: str
    store_id: str
    work_role_id: str | None = None
    work_date: date
    start_time: str  # "HH:MM"
    end_time: str
    break_start_time: str | None = None
    break_end_time: str | None = None
    # 생성 시 적용할 status. 서버 측에서 store.require_approval + actor 권한에 따라
    # 다운그레이드될 수 있음 (Decision #10). draft/requested/confirmed.
    status: str = "confirmed"


class BulkPreviewRequest(BaseModel):
    entries: list[BulkPreviewEntry]


class BulkPreviewItem(BaseModel):
    """유효한 항목 — 예상 비용 포함."""
    index: int
    estimated_cost: float | None = None
    net_work_minutes: int = 0


class BulkPreviewConflict(BaseModel):
    """충돌 항목 — index + 사유."""
    index: int
    message: str


class BulkPreviewWarning(BaseModel):
    """초과근무 경고 — 유저 단위."""
    user_id: str
    type: str  # "overtime"
    total_minutes: int
    limit_minutes: int


class BulkPreviewResponse(BaseModel):
    valid: list[BulkPreviewItem] = []
    conflicts: list[BulkPreviewConflict] = []
    warnings: list[BulkPreviewWarning] = []


# ─── Bulk Update ─────────────────────────────────────


class BulkUpdateItem(BaseModel):
    """단일 수정 항목."""
    id: str
    work_role_id: str | None = None
    start_time: str | None = None  # "HH:MM"
    end_time: str | None = None
    break_start_time: str | None = None
    break_end_time: str | None = None
    # 전환기 datetime 인코딩 — 벌크 시간수정이 주간↔새벽 전환을 표현하도록
    operating_day: date | None = None
    start_at: str | None = None
    end_at: str | None = None
    break_start_at: str | None = None
    break_end_at: str | None = None
    note: str | None = None
    hourly_rate: float | None = None
    reset_checklist: bool | None = None
    # status 변경 (선택). 명시되면 시간 필드 update 후 적절한 전이 함수 호출.
    # draft/requested/confirmed. 권한/현재 status에 따라 거부될 수 있음.
    status: str | None = None


class BulkUpdateRequest(BaseModel):
    updates: list[BulkUpdateItem]


class BulkUpdateResult(BaseModel):
    updated: int = 0
    failed: int = 0
    errors: list[str] = []


# ─── Bulk Delete ─────────────────────────────────────


class BulkDeleteRequest(BaseModel):
    ids: list[str]


class BulkDeleteResult(BaseModel):
    deleted: int = 0
    failed: int = 0
    errors: list[str] = []


# ─── Windowed Roster (Phase 1) ───────────────────────
# 정렬된 staff 로스터 + 필터 반영 행/컬럼 요약. 셀(블록)은 별도(Phase 2 B 엔드포인트).
# 집계단위: TEAM=스케줄 수, 일간 컬럼=30분 점유 0.5 환산, cost=schedule.hourly_rate (GM+ 만).


class RosterRow(BaseModel):
    user_id: str
    user_name: str | None = None
    user_department: str | None = None
    role_priority: int
    # 신규 스케줄 default 표시용 effective rate (GM+ 만; SV 이하는 None 마스킹)
    effective_hourly_rate: float | None = None
    has_schedule_in_period: bool = False
    confirmed_hours: float = 0.0
    pending_hours: float = 0.0
    confirmed_cost: float | None = None  # GM+ 만
    pending_cost: float | None = None


class RosterColumn(BaseModel):
    key: str  # 날짜 "YYYY-MM-DD" (week/month) 또는 "h{n}" (day, n=0..47 overnight 포함)
    team_confirmed: float = 0.0
    team_pending: float = 0.0
    hours_confirmed: float = 0.0
    hours_pending: float = 0.0
    cost_confirmed: float | None = None
    cost_pending: float | None = None
    # day granularity 전용 — 시간당 30분 슬롯 인원. [첫30분(:00–:30), 둘째30분(:30–:00)].
    # week/month 에서는 빈 배열. "슬롯 인원" = 그 30분과 overlap>0 인 스케줄 수(30분 grid라 풀/0).
    slots_confirmed: list[int] = []
    slots_pending: list[int] = []


class RosterTotals(BaseModel):
    team_confirmed: float = 0.0
    team_pending: float = 0.0
    hours_confirmed: float = 0.0
    hours_pending: float = 0.0
    cost_confirmed: float | None = None
    cost_pending: float | None = None
    staff_count: int = 0


class RosterFilterDomain(BaseModel):
    positions: list[str] = []
    shifts: list[str] = []


class RosterResponse(BaseModel):
    roster: list[RosterRow] = []
    columns: list[RosterColumn] = []
    totals: RosterTotals
    filter_domain: RosterFilterDomain
