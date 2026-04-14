"""Settings Registry — default seed entries.

서버 시작 시 settings_registry 테이블에 자동으로 upsert되는 기본 설정 정의 목록.
이미 존재하는 키는 건드리지 않는다 (사용자가 이미 수정했을 수 있으므로).

새 설정을 추가하려면 SETTINGS_SEED 리스트에 한 줄 추가하면 된다.
"""

from typing import Any


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
        description="All requested schedules need GM confirmation before becoming active.",
        value_type="boolean",
        default_value=True,
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
    # ─── Work Rules ────────────────────────────────────
    SettingDefinition(
        key="work.default_schedule_duration_minutes",
        label="Default shift duration (minutes)",
        description="Default schedule length when creating a new schedule. End time = start + this value.",
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
    # ─── Schedule Range ─────────────────────────────────
    SettingDefinition(
        key="schedule.range",
        label="Schedule Range",
        description="Time range displayed on the schedule grid. Defines the start and end hours for the timetable.",
        value_type="json",
        default_value={"all": {"start": "06:00", "end": "23:00"}},
        category="Schedule Range",
    ),
    # ─── Attendance ─────────────────────────────────────
    SettingDefinition(
        key="attendance.late_buffer_minutes",
        label="Late buffer (minutes)",
        description="Grace period after schedule start before clock-in is marked as 'late'.",
        value_type="number",
        default_value=5,
        category="Attendance",
    ),
    SettingDefinition(
        key="attendance.early_leave_threshold_minutes",
        label="Early leave threshold (minutes)",
        description="Clock-out before this many minutes prior to schedule end is marked as 'early leave'.",
        value_type="number",
        default_value=5,
        category="Attendance",
    ),
]
