"""스케줄 시스템 Pydantic 스키마.

Schedule system Pydantic schemas for work roles, break rules, periods, requests, and entries.
"""

from datetime import date, datetime

from pydantic import BaseModel


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
    note: str | None = None


class ScheduleRequestStatusUpdate(BaseModel):
    status: str  # accepted/modified/rejected


class ScheduleRequestUpdate(BaseModel):
    store_id: str | None = None
    work_role_id: str | None = None
    work_date: date | None = None
    preferred_start_time: str | None = None
    preferred_end_time: str | None = None
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
    hourly_rate: float = 0  # 신청 시급 (Resolved: user > store > org)


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
    note: str | None = None


class ScheduleRequestBatchUpdate(BaseModel):
    """배치 제출 - 기존 수정 항목."""
    id: str
    store_id: str | None = None
    work_role_id: str | None = None
    work_date: date | None = None
    preferred_start_time: str | None = None
    preferred_end_time: str | None = None
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
    work_date: date
    start_time: str  # "HH:MM"
    end_time: str
    break_start_time: str | None = None
    break_end_time: str | None = None
    note: str | None = None
    hourly_rate: float | None = None  # 시급 override (optional — auto-calculated if not provided)
    status: str = "confirmed"  # "requested" for app submissions, "confirmed" for direct admin creation
    force: bool = False  # Override warnings


class ScheduleUpdate(BaseModel):
    user_id: str | None = None
    work_role_id: str | None = None
    work_date: date | None = None
    start_time: str | None = None
    end_time: str | None = None
    break_start_time: str | None = None
    break_end_time: str | None = None
    note: str | None = None
    hourly_rate: float | None = None  # 시급 override (optional)
    force: bool = False


class ScheduleResponse(BaseModel):
    id: str
    organization_id: str
    request_id: str | None
    user_id: str
    user_name: str | None = None
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
    net_work_minutes: int
    status: str
    created_by: str | None
    approved_by: str | None
    confirmed_at: datetime | None = None
    note: str | None
    hourly_rate: float = 0  # 확정 시급 (Resolved hourly rate: provided > user > store > org)
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
    """Reject a requested schedule. Reason is required."""
    rejection_reason: str


class ScheduleCancel(BaseModel):
    """Cancel a confirmed schedule (GM+ only). Reason is required."""
    cancellation_reason: str


class ScheduleSwap(BaseModel):
    """Swap two confirmed schedules' assigned users (GM+ only)."""
    other_schedule_id: str
    reason: str | None = None


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
