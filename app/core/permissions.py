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
# Super Owner: 조직 자체의 관리자. 매장 운영 X, 알림 X. 조직당 1명.
SUPER_OWNER_PRIORITY = 5
OWNER_PRIORITY = 10
GM_PRIORITY = 20
SV_PRIORITY = 30
STAFF_PRIORITY = 40


# ── Priority 헬퍼 함수 ────────────────────────────────────
def _priority(user: User) -> int:
    """User 객체에서 priority 추출. role 없으면 999(무권한)."""
    return user.role.priority if user.role else 999


def is_super_owner(user: User) -> bool:
    """Super Owner 여부 (priority <= 5). 조직당 1명, 관리 전용."""
    return _priority(user) <= SUPER_OWNER_PRIORITY


def is_owner(user: User) -> bool:
    """Owner 이상 여부 (priority <= 10). Super Owner 포함."""
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

    # ── Notices ──
    ("notices:read",   "notices", "read",   "View notices", False),
    ("notices:create", "notices", "create", "Create notices", False),
    ("notices:update", "notices", "update", "Edit notices", False),
    ("notices:delete", "notices", "delete", "Delete notices", False),

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

    # ── Evaluations ──
    ("evaluations:read",   "evaluations", "read",   "View evaluation templates and results", False),
    ("evaluations:create", "evaluations", "create", "Create and submit evaluations", False),
    ("evaluations:update", "evaluations", "update", "Edit evaluations", False),
    ("evaluations:delete", "evaluations", "delete", "Delete evaluation templates", False),

    # ── Daily Reports (legacy, multi-type reports로 이관 중) ──
    ("daily_reports:read",   "daily_reports", "read",   "View daily reports", False),
    ("daily_reports:create", "daily_reports", "create", "Write daily reports", False),
    ("daily_reports:update", "daily_reports", "update", "Edit reports and add comments", False),
    ("daily_reports:delete", "daily_reports", "delete", "Delete daily reports", False),

    # ── Reports (multi-type: daily, issue, ...) ──
    ("reports:read",   "reports", "read",   "View reports (all types)", False),
    ("reports:create", "reports", "create", "Write reports", False),
    ("reports:update", "reports", "update", "Edit reports and add comments", False),
    ("reports:delete", "reports", "delete", "Delete reports", False),

    # ── Tasks (work items — promoted from issue reports or directly created) ──
    # 명명 변경 이력: additional_tasks → issues → tasks. issue report 와 단어 겹침을 피하려 tasks 로 정리.
    ("tasks:read",   "tasks", "read",   "View tasks (work items)", False),
    ("tasks:create", "tasks", "create", "Create tasks", False),
    ("tasks:update", "tasks", "update", "Edit / complete tasks", False),
    ("tasks:delete", "tasks", "delete", "Delete tasks", False),

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
    ("org:delete", "org", "delete", "Delete organization (Super Owner only)", False),

    # ── Owner / Super Owner (조직 관리자 전용) ──
    ("owner:assign",         "owner",       "assign",   "Assign Owner role to a user (Super Owner only)", False),
    ("super_owner:transfer", "super_owner", "transfer", "Transfer Super Owner role to another user", False),

    # ── Attendance Devices (공용 근태 기기 관리) ──
    ("attendance_devices:read",   "attendance_devices", "read",   "View attendance terminal devices", False),
    ("attendance_devices:update", "attendance_devices", "update", "Rename, revoke terminal devices, rotate access code", False),

    # ── Clock-in PIN (직원 근태 기기 PIN 관리) ──
    ("clockin_pin:read",   "clockin_pin", "read",   "View staff clock-in PIN (admin lookup)", False),
    ("clockin_pin:update", "clockin_pin", "update", "Regenerate staff clock-in PIN", False),

    # ── Hiring (Form builder + applications) ──
    ("hiring:read",   "hiring", "read",   "View hiring form and applications", False),
    ("hiring:update", "hiring", "update", "Edit hiring form, change application stage", False),
    ("hiring:write",  "hiring", "write",  "Add or edit your own applicant review", False),
    ("hiring:hire",   "hiring", "hire",   "Convert applicant into staff", True),
    ("hiring:block",  "hiring", "block",  "Block candidates from a store", True),

    # ── App Versions (모바일/태블릿 APK 릴리스 카탈로그) ──
    ("app_versions:read",   "app_versions", "read",   "View app release catalog", False),
    ("app_versions:create", "app_versions", "create", "Register new app release (CI integration)", True),

    # ── Tips (팁 입력·분배·신고) ──
    ("tips:read",            "tips", "read",            "View tip entries and distributions", False),
    ("tips:edit_own",        "tips", "edit_own",        "Create and edit own tip entries", False),
    ("tips:edit_all",        "tips", "edit_all",        "Edit any staff tip entries (manager)", False),
    ("tips:add_for_others",  "tips", "add_for_others",  "Add missing tip entries on behalf of staff (manager)", False),
    ("tips:period_confirm",  "tips", "period_confirm",  "Confirm bi-monthly cycle (locks entries)", False),
    ("tips:period_override", "tips", "period_override", "Force-close cycle with reason (audit-trail)", True),
    ("tips:form_view",       "tips", "form_view",       "View IRS Form 4070 documents", False),
]

# 편의용: code → description 조회
PERMISSION_DESCRIPTIONS: dict[str, str] = {
    code: desc for code, _, _, desc, _ in PERMISSION_REGISTRY
}

# 편의용: 전체 코드 목록
ALL_PERMISSION_CODES: set[str] = {code for code, *_ in PERMISSION_REGISTRY}


# Super Owner 전용 권한. Owner/GM/SV/Staff 어디에도 자동 부여되지 않음.
SUPER_OWNER_ONLY: set[str] = {
    "org:delete",
    "owner:assign",
    "super_owner:transfer",
}

# ── 기본 역할별 permission 세팅 ────────────────────────────
# 새 조직 생성(setup) 시 사용. priority 기준.
DEFAULT_ROLE_PERMISSIONS: dict[str, set[str]] = {
    "super_owner": ALL_PERMISSION_CODES,  # 전부 (super_owner 전용 포함)
    "owner": ALL_PERMISSION_CODES - SUPER_OWNER_ONLY,
    "gm": ALL_PERMISSION_CODES - SUPER_OWNER_ONLY - {
        "stores:create", "stores:delete",
        "roles:create", "roles:delete",
        "schedule_history:delete",
        "org:update",
    },
    "sv": {
        "stores:read", "users:read",
        "schedules:read", "schedules:create", "schedules:update",
        "notices:read",
        "checklist_review:read", "checklist_review:create",
        "checklist_log:read",
        "evaluations:read",
        "daily_reports:read", "daily_reports:create", "daily_reports:update",
        "reports:read", "reports:create", "reports:update",
        "tasks:read", "tasks:create", "tasks:update",
        "dashboard:read",
        "inventory:read", "inventory:create",
        "org:read",
        "schedule_history:read",
        # 가이드 §1.1: 사이클 확정은 Owner/GM 위주. SV 는 entry 수정·누락 추가만.
        "tips:read", "tips:edit_own", "tips:edit_all", "tips:add_for_others",
        "tips:form_view",
    },
    "staff": {
        "daily_reports:read", "daily_reports:create", "daily_reports:update",
        "reports:read", "reports:create", "reports:update",
        "tasks:read", "tasks:update",
        "tips:read", "tips:edit_own", "tips:form_view",
    },
}
