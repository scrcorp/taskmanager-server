"""Settings Registry — default seed entries.

서버 시작 시 settings_registry 테이블에 자동으로 upsert되는 기본 설정 정의 목록.
이미 존재하는 키는 건드리지 않는다 (사용자가 이미 수정했을 수 있으므로).

새 설정을 추가하려면 SETTINGS_SEED 리스트에 한 줄 추가하면 된다.

⚠️ **이 시드는 INSERT-only 다** (app/main.py::seed_settings_registry).
기존 키의 default_value/label/description 을 여기서 고쳐도 **DB에 반영되지 않는다.**
이미 배포된 키의 값을 바꾸려면 **UPDATE 마이그레이션을 함께 써야 한다.**
반대로 신규 키는 마이그레이션이 INSERT 주체이고(배포 즉시 반영), 이 시드는
"없으면 넣는다"라 두 경로가 충돌하지 않는다 — 양쪽 정의를 항상 같이 고칠 것.
"""

from typing import Any

# 여러 모듈이 공유하는 키 문자열 — 오타 시 조용히 SettingNotRegisteredError 가 나므로 상수로 고정.
STORE_OPERATING_HOURS_KEY = "store.operating_hours"
SCHEDULE_RANGE_KEY = "schedule.range"
SCHEDULE_REPORT_RECIPIENTS_KEY = "schedule.report_recipients"
SCHEDULE_REPORT_TIMES_KEY = "schedule.report_times"


class SettingDefinition:
    """단일 setting registry 엔트리 정의."""

    def __init__(
        self,
        key: str,
        label: str,
        description: str,
        value_type: str,  # "number" | "boolean" | "string" | "json"
        default_value: Any,
        category: str,
        levels: list[str] | None = None,
        default_priority: str = "item",
        validation_schema: dict | None = None,
    ) -> None:
        self.key = key
        self.label = label
        self.description = description
        self.value_type = value_type
        self.default_value = default_value
        self.category = category
        self.levels = levels or ["org", "store"]
        self.default_priority = default_priority
        self.validation_schema = validation_schema


# ─── Default Settings Catalog ──────────────────────────────────────
# 카테고리 순서대로 정의 (UI에서 같은 순서로 노출됨).

SETTINGS_SEED: list[SettingDefinition] = [
    # ─── Work Hour Alerts ──────────────────────────────
    SettingDefinition(
        key="schedule.work_hour_alert.normal_max",
        label="Normal hours per shift",
        description="Hours considered normal for a single shift. Above this triggers caution warning.",
        value_type="number",
        default_value=5.5,
        category="Work Hour Alerts",
    ),
    SettingDefinition(
        key="schedule.work_hour_alert.caution_max",
        label="Caution hour limit",
        description="Hours after which a shift is flagged as overtime.",
        value_type="number",
        default_value=7.5,
        category="Work Hour Alerts",
    ),
    SettingDefinition(
        key="schedule.max_shift_hours",
        label="Max shift hours",
        description="Single shift above this duration triggers a confirmation warning before saving.",
        value_type="number",
        default_value=16,
        category="Work Hour Alerts",
    ),
    # ─── Weekly Limits ──────────────────────────────────
    SettingDefinition(
        key="schedule.weekly_hour_limit",
        label="Weekly hour limit",
        description="Maximum total scheduled hours per user per week.",
        value_type="number",
        default_value=40,
        category="Weekly Limits",
    ),
    SettingDefinition(
        key="schedule.weekly_hour_warning",
        label="Weekly warning threshold",
        description="Hours per week after which a warning is displayed.",
        value_type="number",
        default_value=35,
        category="Weekly Limits",
    ),
    # ─── Approval Workflow ──────────────────────────────
    SettingDefinition(
        key="schedule.approval_required",
        label="Require GM approval",
        description=(
            "When on, schedules created by SV are submitted as requests and need GM confirmation, "
            "and only GM+ can edit or delete confirmed schedules. "
            "Off by default — SV runs the store day to day and creates confirmed schedules directly."
        ),
        value_type="boolean",
        default_value=False,
        category="Approval Workflow",
    ),
    SettingDefinition(
        key="schedule.auto_confirm_drafts",
        label="Auto-confirm SV drafts",
        description="Automatically confirm schedules created in draft mode by SV+ users.",
        value_type="boolean",
        default_value=False,
        category="Approval Workflow",
    ),
    SettingDefinition(
        key="schedule.allow_staff_request",
        label="Allow staff to request schedules from app",
        description="When enabled, staff can submit schedule requests from the mobile app. When disabled, the app is view-only (read-only schedule display).",
        value_type="boolean",
        default_value=False,
        category="Approval Workflow",
    ),
    # ─── Work Availability ─────────────────────────────
    SettingDefinition(
        key="availability.allow_staff_self",
        label="Allow staff to set their own availability",
        description="When enabled, staff can submit their weekly work availability themselves (via the self-entry link). When disabled, only managers set it in the console; the app stays view-only.",
        value_type="boolean",
        default_value=False,
        category="Work Availability",
    ),
    # ─── Work Rules ────────────────────────────────────
    # 신규 스케줄의 기본 길이 (D8-2). 키를 새로 만들지 않고 이 키를 재사용한다 —
    # 신설하면 같은 의미의 키가 둘이 되고, 더 나쁘게는 워크인 길이와 일반 생성 길이가
    # 갈라져 급여 산정 근거가 경로별로 달라진다.
    # ⚠️ 이 키는 이미 DB에 있으므로 아래 label/description 변경은 UPDATE 마이그레이션으로만 반영된다.
    SettingDefinition(
        key="work.default_schedule_duration_minutes",
        label="Default shift length (minutes)",
        description=(
            "Length applied to a newly created schedule when the work role has no default times. "
            "Also the length of an auto-created walk-in schedule (end time = clock-in time + this value). "
            "Console, kiosk and app all read this one value."
        ),
        value_type="number",
        default_value=330,
        category="Work Rules",
    ),
    SettingDefinition(
        key="break.duration_minutes",
        label="Default break duration (minutes)",
        description="Default break length when splitting a shift.",
        value_type="number",
        default_value=30,
        category="Work Rules",
    ),
    # ─── Store Hours ────────────────────────────────────
    # "시간 범위"를 뜻하는 축이 셋이고 서로 다르다 (D2). 합치지 않는다.
    #   ① 영업일 경계  = 집계일이 바뀌는 시점        → stores.day_start_time 컬럼
    #   ② 영업시간      = 손님에게 여는 시간          → store.operating_hours (아래)
    #   ③ 스케줄 시간대 = 직원이 일할 수 있는 시간대  → schedule.range (아래, 키 이름은 옛 것)
    # 포함 관계 ① ⊇ ③ ⊇ ② 가 곧 검증 규칙이다.
    #
    # 값 형태는 두 키가 공유한다 — 파서(app/utils/timezone.resolve_day_range)도 하나다:
    #   {"mode":"all"|"per_day", "all":{...}, "per_day":{"mon":{...}}, "closed":["mon"]}
    #   각 칸은 {"start":"HH:MM", "end":"HH:MM", "end_offset_days":0|1}
    # 시각은 00:00~23:59 만. 자정 넘김은 end_offset_days 로만 표현한다 (D2-8).
    #
    # **휴무일은 closed 배열 하나로만 표현한다.**
    # per_day 에서 요일 키를 지우는 것은 휴무가 아니라 all 폴백이다.
    SettingDefinition(
        key=STORE_OPERATING_HOURS_KEY,
        label="Store Operating Hours",
        description=(
            "Hours the store is open to customers. Staff working hours are set separately "
            "and normally extend past these (prep and closing). "
            "Days listed as closed still accept schedules, with a warning."
        ),
        value_type="json",
        # 기본값은 **미설정**이다 (`all` 이 빈 칸). 형태만 표준이고 값은 없다.
        # 영업시간은 매장마다 다르니 서버가 지어낼 수 없고, 지어내면 해롭다:
        # 09:00–22:00 같은 값을 기본으로 두면 아직 아무도 설정하지 않은 매장에서
        # 야간 시프트가 영업시간 밖으로 판정돼 **일일 리포트의 인원 부족 검사에서 통째로 빠진다.**
        # "온종일 열림"(00:00→+1d 00:00)도 같은 문제다 — 포함 관계 판정이라
        # 자정을 넘는 시프트는 24시간 창에도 들어가지 못한다.
        # 미설정 = 제한 없음(전부 검사). 좁히려면 사람이 명시적으로 설정한다.
        default_value={"mode": "all", "all": {}, "per_day": {}, "closed": []},
        category="Store Hours",
    ),
    # ⚠️ 키 이름은 옛 것(그리드 "표시 범위")이고, **의미는 D2-4(직원 근무 가능 시간대)** 다.
    # 이름을 바꾸지 않는 이유: 이 키는 settings_registry 의 PK이고 org_settings/store_settings 가
    # FK로 참조한다. 실 데이터가 있는 키를 이름값 때문에 옮기는 것은 이득 대비 위험이 크다 (Q3).
    # 사용자는 화면에서 label 만 보고 키 문자열은 개발자만 본다.
    # SV 공백 판정 기준도 이 값이다 (D2-5) — 직원이 일하는 모든 시간에 SV가 있어야 한다는 규칙.
    SettingDefinition(
        key=SCHEDULE_RANGE_KEY,
        label="Staff Working Hours",
        description=(
            "Hours staff can be scheduled — store operating hours plus prep and closing time. "
            "The schedule grid draws this range, and supervisor coverage gaps are measured against it. "
            "Keep it wider than the operating hours, or the difference is never checked for coverage."
        ),
        value_type="json",
        # 실제 저장값은 {mode, all, per_day, closed} 인데 기본값만 {"all":{...}} 였다.
        # 그 불일치 때문에 클라이언트마다 보정 코드(normalizeRange 등)가 생겼다 — 형태를 맞춰 없앤다.
        default_value={
            "mode": "all",
            "all": {"start": "06:00", "end": "23:00", "end_offset_days": 0},
            "per_day": {},
            # 근무 가능 시간대에는 휴무 개념이 없다. 형태를 영업시간과 맞추기 위해서만 둔다.
            "closed": [],
        },
        category="Store Hours",
    ),
    # ─── Schedule Report ────────────────────────────────
    SettingDefinition(
        key=SCHEDULE_REPORT_RECIPIENTS_KEY,
        label="Schedule report recipients",
        description=(
            "Comma-separated email addresses that receive the daily schedule report. "
            "Leave empty to skip the report for this organization. "
            "Set this per organization — a global list would send one organization's "
            "store names, staff names and hours to another organization's inbox."
        ),
        value_type="string",
        default_value="",
        category="Schedule Report",
        # org 전용. 리포트는 org 단위로 한 통 나가므로 매장별 수신자라는 개념이 없다.
        # 기본값(["org","store"])을 그대로 두면 매장 화면에서 저장은 되는데
        # 아무 효과가 없는 설정이 생긴다 — 조용히 무시되는 UI 는 만들지 않는다.
        levels=["org"],
    ),
    SettingDefinition(
        key=SCHEDULE_REPORT_TIMES_KEY,
        label="Schedule report send times",
        description=(
            "Hours of the day (0-23, comma separated) when the daily schedule report is sent, "
            "in this organization's timezone. Leave empty to stop sending. "
            "Default 7,15,22 — before opening, mid afternoon, and before closing."
        ),
        value_type="string",
        default_value="7,15,22",
        category="Schedule Report",
        levels=["org"],
    ),
    # ─── Attendance ─────────────────────────────────────
    SettingDefinition(
        key="attendance.walk_in_allowed",
        label="Allow walk-in clock-in",
        description="Allow staff to clock in without a pre-created schedule. A walk-in schedule is auto-created on clock-in. When disabled, a confirmed schedule is required.",
        value_type="boolean",
        default_value=False,
        category="Attendance",
    ),
    SettingDefinition(
        key="attendance.tip_entry_enabled",
        label="Tip entry on clock-out",
        description="Show the tip entry screen when staff clock out. When disabled, clock-out completes without asking for tips. Applies to every device in the store.",
        value_type="boolean",
        default_value=False,
        category="Attendance",
    ),
    SettingDefinition(
        key="attendance.auto_clock_out_enabled",
        label="Auto clock-out",
        description="Automatically clock out staff who forgot to clock out. When disabled, this store is skipped by the auto clock-out loop (manager alerts still apply).",
        value_type="boolean",
        default_value=True,
        category="Attendance",
    ),
    # ⚠️ 아래 세 임계값의 default_value 는 app/utils/attendance_judgement.py 의
    # DEFAULT_* 상수(코드 fallback)와 **항상 같은 값**이어야 한다. 갈리면 설정이
    # 등록된 매장과 아닌 매장이 조용히 다르게 동작한다.
    SettingDefinition(
        key="attendance.late_buffer_minutes",
        label="Late buffer (minutes)",
        description="Grace period after schedule start before clock-in is marked as 'late'. Judged to the minute — with a 5 minute buffer, a 17:00 shift is still on time at 17:05 and late from 17:06.",
        value_type="number",
        default_value=5,
        category="Attendance",
    ),
    SettingDefinition(
        key="attendance.early_leave_threshold_minutes",
        label="Early leave threshold (minutes)",
        description="Staff may clock out this many minutes before their schedule ends without a reason. Clocking out earlier asks for a reason and is flagged as 'early leave' — it is never blocked.",
        value_type="number",
        default_value=10,
        category="Attendance",
    ),
    SettingDefinition(
        key="attendance.auto_clock_out_after_minutes",
        label="Auto clock-out delay (minutes)",
        description="Automatically clock out staff who forgot to clock out, this many minutes after their scheduled end. The recorded clock-out time is set to the scheduled end.",
        value_type="number",
        default_value=30,
        category="Attendance",
    ),
    SettingDefinition(
        key="attendance.alert_interval_minutes",
        label="Manager alert interval (minutes)",
        description="When a shift is past its scheduled end without clock-out, alert managers every N minutes (in-app + email).",
        value_type="number",
        default_value=10,
        category="Attendance",
    ),
    SettingDefinition(
        key="attendance.early_clock_in_threshold_minutes",
        label="Early clock-in threshold (minutes)",
        description="Staff may clock in this many minutes before their shift starts without a reason. Clocking in earlier asks for a reason and is flagged for the manager to review — it is never blocked, and there is no limit on how early.",
        value_type="number",
        default_value=10,
        category="Attendance",
    ),
]
