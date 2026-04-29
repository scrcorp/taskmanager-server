"""권한 시스템 상수 및 헬퍼 — Permission system constants and helpers.

모든 priority 비교는 이 모듈의 상수/함수를 사용한다.
매직넘버(10, 20, 30, 40) 직접 비교 금지.

Usage:
    from app.core.permissions import is_owner, is_gm_plus, hide_cost_for_priority

    if is_owner(user):
        ...
    if hide_cost_for_priority(user.role.priority):
        ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.user import User

# ── Priority 상수 ──────────────────────────────────────────
# 낮을수록 높은 권한. DB roles.priority와 동일.
OWNER_PRIORITY = 10
GM_PRIORITY = 20
SV_PRIORITY = 30
STAFF_PRIORITY = 40


# ── Priority 헬퍼 함수 ────────────────────────────────────
def _priority(user: User) -> int:
    """User 객체에서 priority 추출. role 없으면 999(무권한)."""
    return user.role.priority if user.role else 999


def is_owner(user: User) -> bool:
    """Owner 여부 (priority <= 10)."""
    return _priority(user) <= OWNER_PRIORITY


def is_gm_plus(user: User) -> bool:
    """GM 이상 여부 (priority <= 20). Owner 포함."""
    return _priority(user) <= GM_PRIORITY


def is_sv_plus(user: User) -> bool:
    """SV 이상 여부 (priority <= 30). Owner, GM 포함."""
    return _priority(user) <= SV_PRIORITY


def hide_cost_for_priority(priority: int) -> bool:
    """cost(시급) 정보를 숨겨야 하는지. SV 이하(priority > 20)면 True."""
    return priority > GM_PRIORITY


# ── Permission Registry ──────────────────────────────────────
# 유일한 진실의 원천(Single Source of Truth).
# 새 기능에 권한이 필요하면:
#   1. 이 REGISTRY를 먼저 확인
#   2. 있으면 그대로 사용, 없으면 여기에 추가 후 사용
#   3. REGISTRY 밖에서 permission 코드 문자열을 임의 생성 금지
#
# 서버 시작 시 DB permissions 테이블과 자동 동기화됨.
# (code, resource, action, description, require_priority_check)

PERMISSION_REGISTRY: list[tuple[str, str, str, str, bool]] = [
    # ── Stores ──
    ("stores:read",    "stores", "read",   "View store list and details", False),
    ("stores:create",  "stores", "create", "Create new stores", False),
    ("stores:update",  "stores", "update", "Edit store information", False),
    ("stores:delete",  "stores", "delete", "Delete stores", False),

    # ── Users ──
    ("users:read",           "users", "read",           "View staff list and profiles", False),
    ("users:create",         "users", "create",         "Create staff accounts", True),
    ("users:update",         "users", "update",         "Edit staff information", True),
    ("users:delete",         "users", "delete",         "Delete staff accounts", True),
    ("users:reset_password", "users", "reset_password", "Reset staff passwords", True),

    # ── Roles ──
    ("roles:read",   "roles", "read",   "View roles and permissions", False),
    ("roles:create", "roles", "create", "Create new roles", True),
    ("roles:update", "roles", "update", "Edit roles and permission matrix", True),
    ("roles:delete", "roles", "delete", "Delete roles", True),

    # ── Schedules ──
    ("schedules:read",    "schedules", "read",    "View schedule list and details", False),
    ("schedules:create",  "schedules", "create",  "Create schedules", False),
    ("schedules:update",  "schedules", "update",  "Edit schedule details", False),
    ("schedules:delete",  "schedules", "delete",  "Delete schedules", False),
    ("schedules:approve", "schedules", "approve", "Confirm requested schedules", False),
    ("schedules:cancel",  "schedules", "cancel",  "Cancel confirmed schedules", False),
    ("schedules:revert",  "schedules", "revert",  "Revert confirmed back to requested", False),

    # ── Schedule History ──
    ("schedule_history:read",   "schedule_history", "read",   "View schedule change history", False),
    ("schedule_history:delete", "schedule_history", "delete", "Delete history entries", False),

    # ── Schedule Settings ──
    ("schedule_settings:manage", "schedule_settings", "manage", "Access and modify schedule settings", False),

    # ── Announcements ──
    ("announcements:read",   "announcements", "read",   "View announcements", False),
    ("announcements:create", "announcements", "create", "Create announcements", False),
    ("announcements:update", "announcements", "update", "Edit announcements", False),
    ("announcements:delete", "announcements", "delete", "Delete announcements", False),

    # ── Checklists ──
    ("checklists:read",   "checklists", "read",   "View checklist templates and instances", False),
    ("checklists:create", "checklists", "create", "Create checklist templates", False),
    ("checklists:update", "checklists", "update", "Edit checklist templates", False),
    ("checklists:delete", "checklists", "delete", "Delete checklist templates", False),

    # ── Checklist Review ──
    ("checklist_review:read",   "checklist_review", "read",   "View checklist reviews and scores", False),
    ("checklist_review:create", "checklist_review", "create", "Write reviews and assign scores", False),
    ("checklist_review:delete", "checklist_review", "delete", "Delete reviews", False),

    # ── Checklist Log ──
    ("checklist_log:read", "checklist_log", "read", "View checklist completion logs", False),

    # ── Tasks ──
    ("tasks:read",   "tasks", "read",   "View additional tasks", False),
    ("tasks:create", "tasks", "create", "Create additional tasks", False),
    ("tasks:update", "tasks", "update", "Edit additional tasks", False),
    ("tasks:delete", "tasks", "delete", "Delete additional tasks", False),

    # ── Evaluations ──
    ("evaluations:read",   "evaluations", "read",   "View evaluation templates and results", False),
    ("evaluations:create", "evaluations", "create", "Create and submit evaluations", False),
    ("evaluations:update", "evaluations", "update", "Edit evaluations", False),
    ("evaluations:delete", "evaluations", "delete", "Delete evaluation templates", False),

    # ── Daily Reports ──
    ("daily_reports:read",   "daily_reports", "read",   "View daily reports", False),
    ("daily_reports:create", "daily_reports", "create", "Write daily reports", False),
    ("daily_reports:update", "daily_reports", "update", "Edit reports and add comments", False),
    ("daily_reports:delete", "daily_reports", "delete", "Delete daily reports", False),

    # ── Dashboard ──
    ("dashboard:read", "dashboard", "read", "View dashboard statistics", False),

    # ── Inventory ──
    ("inventory:read",   "inventory", "read",   "View inventory and stock levels", False),
    ("inventory:create", "inventory", "create", "Add products and process stock in/out", False),
    ("inventory:update", "inventory", "update", "Edit inventory items", False),
    ("inventory:delete", "inventory", "delete", "Delete inventory items", False),

    # ── Cost ──
    ("cost:read",   "cost", "read",   "View hourly rates across schedules and staff", False),
    ("cost:update", "cost", "update", "Modify hourly rates", False),

    # ── Organization ──
    ("org:read",   "org", "read",   "View organization info and settings", False),
    ("org:update", "org", "update", "Modify organization settings", False),

    # ── Attendance Devices (공용 근태 기기 관리) ──
    ("attendance_devices:read",   "attendance_devices", "read",   "View attendance terminal devices", False),
    ("attendance_devices:update", "attendance_devices", "update", "Rename, revoke terminal devices, rotate access code", False),

    # ── Clock-in PIN (직원 근태 기기 PIN 관리) ──
    ("clockin_pin:read",   "clockin_pin", "read",   "View staff clock-in PIN (admin lookup)", False),
    ("clockin_pin:update", "clockin_pin", "update", "Regenerate staff clock-in PIN", False),

    # ── Hiring (Form builder + applications) ──
    ("hiring:read",   "hiring", "read",   "View hiring form and applications", False),
    ("hiring:update", "hiring", "update", "Edit hiring form, change application stage", False),
    ("hiring:hire",   "hiring", "hire",   "Convert applicant into staff", True),
    ("hiring:block",  "hiring", "block",  "Block candidates from a store", True),

    # ── App Versions (모바일/태블릿 APK 릴리스 카탈로그) ──
    ("app_versions:read",   "app_versions", "read",   "View app release catalog", False),
    ("app_versions:create", "app_versions", "create", "Register new app release (CI integration)", True),
]

# 편의용: code → description 조회
PERMISSION_DESCRIPTIONS: dict[str, str] = {
    code: desc for code, _, _, desc, _ in PERMISSION_REGISTRY
}

# 편의용: 전체 코드 목록
ALL_PERMISSION_CODES: set[str] = {code for code, *_ in PERMISSION_REGISTRY}


# ── 기본 역할별 permission 세팅 ────────────────────────────
# 새 조직 생성(setup) 시 사용. priority 기준.
DEFAULT_ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": ALL_PERMISSION_CODES,  # 전부
    "gm": ALL_PERMISSION_CODES - {
        "stores:create", "stores:delete",
        "roles:create", "roles:delete",
        "schedule_history:delete",
        "org:update",
    },
    "sv": {
        "stores:read", "users:read",
        "schedules:read", "schedules:create", "schedules:update",
        "announcements:read",
        "checklists:read",
        "checklist_review:read", "checklist_review:create",
        "checklist_log:read",
        "tasks:read",
        "evaluations:read",
        "daily_reports:read", "daily_reports:create", "daily_reports:update",
        "dashboard:read",
        "inventory:read", "inventory:create",
        "org:read",
        "schedule_history:read",
    },
    "staff": {
        "daily_reports:read", "daily_reports:create", "daily_reports:update",
    },
}
