"""관리자 API 라우터 패키지 — 모든 관리자 엔드포인트 통합.

Admin API Router package — Aggregates all admin-facing endpoints
into a single router for inclusion in the FastAPI application.

Included routers (Phase 1 — Foundation):
    - auth: 관리자 인증 (Admin authentication)
    - organizations: 조직 관리 (Organization management)
    - stores: 매장 관리 (Store management)
    - roles: 역할 관리 (Role management)
    - users: 사용자 관리 (User management)
    - shifts: 근무조 관리 (Shift management under stores)
    - positions: 직책 관리 (Position management under stores)

Included routers (Phase 2 — Core Workflow):
    - checklists: 체크리스트 템플릿/항목 관리 (Checklist template & item management)
    - assignments: 업무 배정 관리 (Work assignment management)

Included routers (Phase 3 — Communication):
    - notices: 공지사항 관리 (Notice management)
    - tasks: 추가 업무 관리 (Additional task management)
    - alerts: 관리자 알림 관리 (Admin alert management)

Included routers (Phase 4 — Schedule):
    - schedules: 스케줄 관리 (Schedule draft & approval management)

Included routers (Phase 5 — Attendance):
    - attendances: 근태 기록 관리 (Attendance record management)
    - qr_codes: QR 코드 관리 (QR code management for attendance scanning)
"""

from fastapi import APIRouter

# Phase 1 — Foundation 라우터 임포트
from app.api.console.auth import router as auth_router
from app.api.console.organizations import router as organizations_router
from app.api.console.super_owner import router as super_owner_router
from app.api.console.stores import router as stores_router
from app.api.console.roles import router as roles_router
from app.api.console.users import router as users_router
from app.api.console.shifts import router as shifts_router
from app.api.console.positions import router as positions_router

# Phase 2 — Core Workflow 라우터 임포트
from app.api.console.checklists import router as checklists_router
# assignments 라우터 제거됨 — schedule 시스템으로 대체
from app.api.console.checklist_instances import router as checklist_instances_router

# Phase 3 — Communication 라우터 임포트
from app.api.console.notices import router as notices_router
from app.api.console.alerts import router as alerts_router
from app.api.console.profile import router as profile_router

# Phase 4 — Schedule (구 schedules 라우터 삭제됨, schedule_entries가 /schedules로 이동)

# Phase 5 — Attendance 라우터 임포트
from app.api.console.attendances import router as attendances_router
from app.api.console.attendance_actions import router as attendance_actions_router
from app.api.console.qr_codes import router as qr_codes_router
from app.api.console.attendance_devices import router as attendance_devices_router

# Phase 6 — Store Extensions 라우터 임포트
from app.api.console.shift_presets import router as shift_presets_router
from app.api.console.labor_law import router as labor_law_router

# Phase 7 — Evaluation 라우터 임포트
from app.api.console.evaluations import router as evaluations_router

# Phase 8 — Dashboard 라우터 임포트
from app.api.console.dashboard import router as dashboard_router

# Phase 9 — Template Links 라우터 임포트
from app.api.console.template_links import router as template_links_router

# Phase 10 — Voices 라우터 임포트
from app.api.console.voices import router as voices_router

# Schedule System — Work Roles + Break Rules + Schedules 라우터 임포트
from app.api.console.work_roles import router as work_roles_router
from app.api.console.break_rules import router as break_rules_router
from app.api.console.schedule_requests import router as schedule_requests_router
from app.api.console.schedules import router as schedule_entries_router

# Daily Reports 라우터 임포트 (legacy)
from app.api.console.daily_reports import router as daily_reports_router
from app.api.console.daily_report_templates import router as daily_report_templates_router

# Reports 라우터 임포트 (multi-type)
from app.api.console.reports import router as reports_router
from app.api.console.report_templates import router as report_templates_router

# Tasks 라우터 임포트 (renamed from additional_tasks → issues → tasks)
from app.api.console.tasks import router as tasks_router

# Storage 라우터 임포트
from app.api.console.storage import router as storage_router

# Permission 관리 라우터 임포트
from app.api.console.permissions import router as permissions_router

# Inventory 라우터 임포트
from app.api.console.inventory import router as inventory_router

# Bulk Upload 라우터 임포트
from app.api.console.bulk_upload import router as bulk_upload_router

# App Versions (sideload APK 릴리스 카탈로그) 라우터 임포트
from app.api.console.app_versions import router as app_versions_router

# Tips (매니저용 — Stage A: Review / Distributions)
from app.api.console.tips import router as console_tips_router

# Schedule Daily Report (수동 트리거) 라우터 임포트
from app.api.console.schedule_reports import router as schedule_reports_router

console_router: APIRouter = APIRouter()

# ---------------------------------------------------------------------------
# Phase 1 라우터 등록 — Register Phase 1 (Foundation) routers
# ---------------------------------------------------------------------------
console_router.include_router(auth_router, prefix="/auth", tags=["Console Auth"])
console_router.include_router(organizations_router, prefix="/organizations", tags=["Organizations"])
console_router.include_router(super_owner_router, prefix="/super-owner", tags=["Super Owner"])
console_router.include_router(stores_router, prefix="/stores", tags=["Stores"])
console_router.include_router(roles_router, prefix="/roles", tags=["Roles"])
console_router.include_router(users_router, prefix="/users", tags=["Users"])
console_router.include_router(shifts_router, tags=["Shifts"])
console_router.include_router(positions_router, tags=["Positions"])

# ---------------------------------------------------------------------------
# Phase 2 라우터 등록 — Register Phase 2 (Core Workflow) routers
# ---------------------------------------------------------------------------
# 체크리스트: /stores/{store_id}/checklist-templates 형태 (nested under stores)
console_router.include_router(checklists_router, tags=["Checklists"])
# 업무 배정 라우터 제거됨 — schedule 시스템으로 대체 (/admin/schedules 사용)
# 체크리스트 인스턴스: /checklist-instances 하위 (Checklist instances)
console_router.include_router(checklist_instances_router, prefix="/checklist-instances", tags=["Checklist Instances"])

# ---------------------------------------------------------------------------
# Phase 3 라우터 등록 — Register Phase 3 (Communication) routers
# ---------------------------------------------------------------------------
console_router.include_router(notices_router, prefix="/notices", tags=["Notices"])
console_router.include_router(alerts_router, prefix="/alerts", tags=["Admin Alerts"])
console_router.include_router(profile_router, prefix="/profile", tags=["Admin Profile"])

# ---------------------------------------------------------------------------
# Phase 4 라우터 등록 — Register Phase 4 (Schedule) routers
# ---------------------------------------------------------------------------
# (구 schedules 라우터 삭제됨 — schedule_entries가 /schedules로 이동)

# ---------------------------------------------------------------------------
# Phase 5 라우터 등록 — Register Phase 5 (Attendance) routers
# ---------------------------------------------------------------------------
# 근태: /attendances 하위 (Attendance records)
console_router.include_router(attendances_router, prefix="/attendances", tags=["Attendances"])
# 근태 액션: /attendances/{id}/actions/* (state-machine transitions)
console_router.include_router(attendance_actions_router, prefix="/attendances", tags=["Attendance Actions"])
# QR 코드: /stores/{store_id}/qr-codes 및 /qr-codes 하위 (QR code management)
console_router.include_router(qr_codes_router, tags=["QR Codes"])
console_router.include_router(attendance_devices_router, tags=["Attendance Devices"])

# ---------------------------------------------------------------------------
# Phase 6 라우터 등록 — Register Phase 6 (Store Extensions) routers
# ---------------------------------------------------------------------------
# 시프트 프리셋: /stores/{store_id}/shift-presets (nested under stores)
console_router.include_router(shift_presets_router, tags=["Shift Presets"])
# 노동법 설정: /stores/{store_id}/labor-law (nested under stores)
console_router.include_router(labor_law_router, tags=["Labor Law"])

# ---------------------------------------------------------------------------
# Phase 7 라우터 등록 — Register Phase 7 (Evaluation) routers
# ---------------------------------------------------------------------------
# 평가: /evaluations 하위 (Evaluation templates & evaluations)
console_router.include_router(evaluations_router, prefix="/evaluations", tags=["Evaluations"])

# ---------------------------------------------------------------------------
# Phase 8 라우터 등록 — Register Phase 8 (Dashboard) routers
# ---------------------------------------------------------------------------
# 대시보드: /dashboard 하위 (Dashboard aggregation APIs)
console_router.include_router(dashboard_router, prefix="/dashboard", tags=["Dashboard"])

# ---------------------------------------------------------------------------
# Phase 9 라우터 등록 — Register Phase 9 (Template Links) routers
# ---------------------------------------------------------------------------
# 템플릿 연결: /checklist-template-links 하위
console_router.include_router(template_links_router, tags=["Checklist Template Links"])

# ---------------------------------------------------------------------------
# Phase 10 라우터 등록 — Register Phase 10 (Voices) routers
# ---------------------------------------------------------------------------
console_router.include_router(voices_router, prefix="/voices", tags=["Voices"])

# ---------------------------------------------------------------------------
# Permission 관리 라우터 등록
# ---------------------------------------------------------------------------
console_router.include_router(permissions_router, prefix="/permissions", tags=["Permissions"])

# ---------------------------------------------------------------------------
# Daily Reports 라우터 등록 — Register Daily Reports routers
# ---------------------------------------------------------------------------
console_router.include_router(daily_reports_router, prefix="/daily-reports", tags=["Daily Reports"])
console_router.include_router(daily_report_templates_router, prefix="/daily-report-templates", tags=["Daily Report Templates"])
console_router.include_router(reports_router, prefix="/reports", tags=["Reports"])
console_router.include_router(report_templates_router, prefix="/report-templates", tags=["Report Templates"])
console_router.include_router(tasks_router, prefix="/tasks", tags=["Tasks"])

# ---------------------------------------------------------------------------
# Schedule System 라우터 등록 — Work Roles + Break Rules
# ---------------------------------------------------------------------------
console_router.include_router(work_roles_router, tags=["Work Roles"])
console_router.include_router(break_rules_router, tags=["Break Rules"])
console_router.include_router(schedule_requests_router, prefix="/schedule-requests", tags=["Schedule Requests"])
console_router.include_router(schedule_entries_router, prefix="/schedules", tags=["Schedules"])

# Settings (Registry + Org/Store/Staff overrides)
from app.api.console.settings import router as settings_router  # noqa: E402
console_router.include_router(settings_router, prefix="/settings", tags=["Settings"])

# ---------------------------------------------------------------------------
# Storage 라우터 등록 — S3 presigned URL
# ---------------------------------------------------------------------------
console_router.include_router(storage_router, prefix="/storage", tags=["Storage"])

# ---------------------------------------------------------------------------
# Tips 라우터 등록 — 매니저용 entries/distributions (Stage A)
# ---------------------------------------------------------------------------
console_router.include_router(console_tips_router, prefix="/tips", tags=["Tips"])

# ---------------------------------------------------------------------------
# Inventory 라우터 등록 — Register Inventory routers
# ---------------------------------------------------------------------------
console_router.include_router(inventory_router, tags=["Inventory"])

# ---------------------------------------------------------------------------
# Bulk Upload 라우터 등록 — Register Bulk Upload routers
# ---------------------------------------------------------------------------
console_router.include_router(bulk_upload_router, prefix="/bulk", tags=["Bulk Upload"])

# ---------------------------------------------------------------------------
# Hiring 라우터 등록
# ---------------------------------------------------------------------------
from app.api.console.hiring import router as hiring_router  # noqa: E402
console_router.include_router(hiring_router, tags=["Admin Hiring"])

# ---------------------------------------------------------------------------
# App Versions 라우터 등록 — sideload APK 릴리스 카탈로그
# ---------------------------------------------------------------------------
console_router.include_router(app_versions_router, prefix="/app-versions", tags=["App Versions"])

# ---------------------------------------------------------------------------
# Schedule Daily Report 라우터 등록 — Owner 전용 수동 트리거
# ---------------------------------------------------------------------------
console_router.include_router(schedule_reports_router, prefix="/schedule-report", tags=["Schedule Report"])
