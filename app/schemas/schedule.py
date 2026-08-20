"""스케줄 시스템 Pydantic 스키마.

Schedule system Pydantic schemas for work roles, break rules, and entries.
"""

import re
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

# 스케줄 시간은 grid 단위로만 허용. 어긋나면 reject (반올림하지 않음).
#
# **이 상수가 시각 입력 단위의 유일한 출처다** (D6).
# 예전에는 console/벌크 30분, 키오스크 5분으로 갈라져 있었다. 그 차등 때문에
#   - 같은 위반이 경로에 따라 400/422 로 다르게 나갔고
#   - 프리뷰(기본 30분)와 저장(키오스크 5분)의 판정이 어긋났으며
#   - 앱이 종료를 23:59 로 clamp 하면 어느 단위로도 배수가 아니어서 저녁 스케줄 생성이 막혔다.
# 5분으로 통일한다. 기존 30분 데이터는 전부 5의 배수라 마이그레이션이 필요 없다.
#
# 표시 슬롯과는 별개다 — 콘솔 그리드는 30분 행 렌더링을 유지한다(블록 위치는 분 단위 overlap 계산).
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
SCHEDULE_STEP_MINUTES = 5


def grid_error_message() -> str:
    """step 위반 시 사용자에게 보여줄 문구 — 서버가 단일 출처."""
    return f"Time must be in {SCHEDULE_STEP_MINUTES}-minute increments."


def validate_grid(value: str | None) -> str | None:
    """"HH:MM" 가 5분 단위인지 검증. None/"" 은 통과 (optional 필드).

    ⚠️ 스키마 validator 로 쓰지 말 것. grid 판정의 단일 관문은
    `schedule_service._normalize_shift_input` 이다 — 스키마에서 또 걸면
    같은 위반이 422 로도 나가고, 워크인처럼 기존에 저장된 비배수 값을
    수정조차 할 수 없게 된다(D7: 검사 대상은 이번에 바뀐 값뿐).
    이 함수는 그 관문과 입력 파싱에서만 쓴다.
    """
    if value is None or value == "":
        return value
    m = _HHMM_RE.match(value)
    if not m:
        raise ValueError("Time must be in HH:MM format.")
    if int(m.group(2)) % SCHEDULE_STEP_MINUTES != 0:
        raise ValueError(grid_error_message())
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


# ─── 폐기: 신청(request) 스키마 ───────────────────────
# 스케줄 신청 기능 폐기(2026-08-09)와 함께 제거.
# `status='requested'` 자체는 승인 절차용으로 남아 있으며, SV 가 스케줄을 만들 때
# `schedule_service.create_entry` 가 승인 설정에 따라 다운그레이드한다(D10-4).
# 폐기된 것은 "신청을 만드는 별도 경로"이지 승인 대기 상태가 아니다.

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
    # 시작 달력일을 **사람이 화면에서 직접 골랐다**는 의사표시.
    # 날짜는 (영업일, 시작 시각, 매장 경계)에서 하나로 결정되는 파생값이라, 표시 없이
    # 자동값과 다른 날짜가 오면 클라 버그/옛 오프셋 잔재로 보고 차단한다(START_DATE_MISMATCH).
    # 화면에서 후보를 고른 경우에만 true 로 실어 보낸다 — 그때만 경고(확인 후 진행)가 된다.
    date_override: bool = False

    # 시각 grid 검증은 여기서 하지 않는다 — 판정의 단일 관문은
    # schedule_service._normalize_shift_input 이다(D6-4).


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
    # 시작 달력일을 사람이 직접 골랐다는 의사표시 (ScheduleCreate 와 같은 의미).
    date_override: bool = False
    # 수정 사유 — schedule_audit_logs.reason 에 그대로 기록된다 (History 노출).
    # attendance correction 은 reason 이 필수인데 schedule 수정만 사유 없이 diff 만 남아서,
    # "왜 바꿨나" 를 History 에서 알 수 없었다. 선택 입력 (기존 호출자 호환).
    change_reason: str | None = Field(default=None, max_length=500)
    reset_checklist: bool | None = None
    # user_id 변경 시 기존 체크리스트 처리:
    # None  = 충돌(in_progress/completed) 시 에러 반환 (프론트가 선택 후 재요청)
    # True  = 체크리스트 초기화
    # False = 진행 상태 그대로 유지

    # 시각 grid 검증은 _normalize_shift_input 단일 관문에서 (ScheduleCreate 주석 참조).


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
    # 신 인코딩이 SoT. 위 구 필드(work_date/start_time 등)는 DB 컬럼이 아니라 모델
    # read-only shim에서 계산 방출 — 옛 앱 하위호환(D2). Wave 4(강제 업데이트)에서 제거.
    operating_day: date | None = None
    start_at: str | None = None  # "YYYY-MM-DDTHH:MM" 벽시계
    end_at: str | None = None
    break_start_at: str | None = None
    break_end_at: str | None = None
    net_work_minutes: int
    # 이 스케줄의 시작이 **자기 영업일 구간 밖**인가 (데이터 이상 신호).
    #
    # 영업일 D 의 구간은 `[day_start(D), day_start(D+1))` 이고, 시작은 그 안에 있어야 한다.
    # 지금은 저장 단계에서 막지만(START_DATE_MISMATCH) **이미 저장된 행**, SQL 직접 수정,
    # 임포트, 그리고 **매장 경계 설정을 나중에 바꾼 경우**는 그 검증을 지나가지 않는다.
    # 그런 행은 현장에서 출근이 안 되므로(후보 조회에 안 잡힌다) 화면이 **이상하다고
    # 표시**해서 사람이 고칠 수 있어야 한다. 조용히 정상처럼 보이는 것이 가장 나쁘다.
    start_outside_operating_window: bool = False
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
    # 저장은 됐지만 확인이 필요한 항목들 (겹침·초과근무·영업일 경계 등).
    # 비어 있으면 경고 없이 저장된 것.
    warnings: list["BulkEntryWarnings"] = []


class ScheduleIssue(BaseModel):
    """검증 항목 하나 — 코드 + 파라미터 (D9-4).

    문구는 클라이언트가 code 로 구성한다. 서버 문자열을 매칭하지 말 것.
    코드 목록은 `app/core/schedule_codes.py` 가 단일 출처.
    """
    code: str
    params: dict = {}


class BulkEntryWarnings(BaseModel):
    """벌크 항목 하나에 붙은 경고 — 요청 배열의 index 로 어떤 항목인지 지목한다.

    문구는 클라이언트가 code 로 구성한다(D9-4). index 를 주는 이유는
    "어떤 직원의 어느 날 근무인지"를 클라이언트가 자기 요청 배열에서
    바로 찾아 보여줄 수 있게 하기 위함이다 — 서버 문장에 이름/날짜를
    끼워 넣는 것보다 정확하고, 번역·표기도 클라이언트 쪽에 남는다.
    """
    index: int
    warnings: list[ScheduleIssue] = []


class ScheduleValidation(BaseModel):
    """프리뷰(/schedules/validate) 응답.

    프리뷰는 "저장 시도"가 아니라 질의이므로 **항상 200** 이다(N3).
    저장 경로의 400/409 와 상태 코드를 맞추지 않는다.
    """
    valid: bool
    warnings: list[ScheduleIssue] = []
    errors: list[ScheduleIssue] = []


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
    """충돌 항목 — index + 사유.

    `message` 는 fallback 이고, 새 클라이언트는 `errors` 의 code 로 문구를 구성한다(D9-4).
    """
    index: int
    message: str
    errors: list[ScheduleIssue] = []


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
    # 시작 달력일을 사람이 직접 골랐다는 의사표시 (ScheduleCreate 와 같은 의미).
    # 벌크에서도 필요하다 — 여기가 비면 다건 경로만 검증 강도가 달라진다.
    date_override: bool = False
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
    # 조회 기간에 근무 기록이 있어 남긴 비활성(퇴사·배정해제) 행인지.
    # 활성자는 False. 미래 배정 후보에서는 제외되지만 과거 조회에서는 보여야 한다.
    is_inactive: bool = False
    # 배정 가능 범위 — 화면이 **날짜 단위로** 칸을 잠그기 위한 값 (2026-08-19).
    #   assignable=False        → 어떤 날짜도 불가 (모든 칸 잠금)
    #   assignable_until=None   → 제한 없음
    #   assignable_until="D"    → D 까지(당일 포함) 가능, 다음날부터 잠금
    # 서버 저장 검증과 **같은 판정 함수**(staff_assignment_service)에서 나온다.
    assignable: bool = True
    assignable_until: str | None = None
    # 표시용 재직기간 — "언제부터 언제까지 일했나". 판정(assignable_until)과 별개 축이다.
    # [보류 2026-08-19] 콘솔은 이 값을 **아직 표시하지 않는다.** 퇴사는 매장 단위 개념인데
    # 여기 실리는 날짜는 org 단위라, 그대로 띄우면 다른 매장 재직자를 잘못 말한다.
    # 매장별 입·퇴사일이 생기면 이 필드가 그 값을 싣고 화면이 다시 켜진다.
    # 설계: docs/99_inbox/2026-08-19-퇴사-매장별-재정의.md
    employed_from: str | None = None
    employed_to: str | None = None
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
