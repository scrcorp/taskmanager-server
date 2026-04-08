"""앱 API 라우터 패키지 — 모든 앱(직원용) 엔드포인트 통합.

App API Router package — Aggregates all app-facing (employee) endpoints
into a single router for inclusion in the FastAPI application.

Included routers (Phase 1 — Foundation):
    - auth: 앱 인증 (App authentication and registration)
    - profile: 내 프로필 조회/수정 (My profile read/update)

Included routers (Phase 2 — Core Workflow):
    - assignments: 내 업무 배정 (My work assignments)

Included routers (Phase 3 — Communication):
    - announcements: 내 공지사항 (My announcements)
    - tasks: 내 추가 업무 (My additional tasks)
    - notifications: 내 알림 (My notifications)

Included routers (Phase 5 — Attendance):
    - attendances: 내 근태 (My attendance: QR scan, today, history)
"""

from fastapi import APIRouter

# Phase 1 — Foundation 라우터 임포트
from app.api.app.auth import router as auth_router
from app.api.app.profile import router as profile_router

# Phase 2 — Core Workflow 라우터 임포트
# assignments 라우터 제거됨 — /my/schedules 엔드포인트로 대체
from app.api.app.checklist_instances import router as checklist_instances_router

# Phase 3 — Communication 라우터 임포트
from app.api.app.announcements import router as announcements_router
from app.api.app.tasks import router as tasks_router
from app.api.app.notifications import router as notifications_router

# Phase 5 — Attendance 라우터 임포트
from app.api.app.attendances import router as attendance_router

# Phase — Daily Reports 라우터 임포트
from app.api.app.daily_reports import router as daily_reports_router

# 매장 목록 라우터 임포트
from app.api.app.stores import router as stores_router

# Phase — Storage 라우터 임포트
from app.api.app.storage import router as storage_router

# Phase 10 — Voices 라우터 임포트
from app.api.app.voices import router as voices_router

# Schedule System — Schedule Requests + Templates + Work Roles 라우터 임포트
from app.api.app.schedule_requests import router as schedule_requests_router
from app.api.app.request_templates import router as request_templates_router
from app.api.app.work_roles import router as app_work_roles_router
from app.api.app.schedules import router as schedule_entries_router

from app.config import settings


app_router: APIRouter = APIRouter()


@app_router.get("/config", tags=["App Config"])
async def get_app_config() -> dict:
    """Return platform-level configuration for the app client."""
    return {
        "max_photos_per_item": settings.MAX_PHOTOS_PER_ITEM,
    }

# ---------------------------------------------------------------------------
# Phase 1 라우터 등록 — Register Phase 1 (Foundation) routers
# ---------------------------------------------------------------------------
app_router.include_router(auth_router, prefix="/auth", tags=["App Auth"])
# 프로필: /profile 엔드포인트 (GET/PUT my profile)
app_router.include_router(profile_router, tags=["App Profile"])

# ---------------------------------------------------------------------------
# Phase 2 라우터 등록 — Register Phase 2 (Core Workflow) routers
# ---------------------------------------------------------------------------
# 내 업무 배정 라우터 제거됨 — /my/schedules 엔드포인트로 대체
# 내 체크리스트 인스턴스: /my/checklist-instances 하위 (My checklist instances)
app_router.include_router(checklist_instances_router, prefix="/my/checklist-instances", tags=["My Checklists"])

# ---------------------------------------------------------------------------
# Phase 3 라우터 등록 — Register Phase 3 (Communication) routers
# ---------------------------------------------------------------------------
app_router.include_router(announcements_router, prefix="/my/announcements", tags=["My Announcements"])
app_router.include_router(tasks_router, prefix="/my/additional-tasks", tags=["My Tasks"])
app_router.include_router(notifications_router, prefix="/my/notifications", tags=["My Notifications"])

# ---------------------------------------------------------------------------
# Phase 5 라우터 등록 — Register Phase 5 (Attendance) routers
# ---------------------------------------------------------------------------
# 내 근태: /my/attendance 하위 (My attendance: QR scan, today, history)
app_router.include_router(attendance_router, prefix="/my/attendance", tags=["My Attendance"])

# ---------------------------------------------------------------------------
# Daily Reports 라우터 등록 — Register Daily Reports routers
# ---------------------------------------------------------------------------
app_router.include_router(daily_reports_router, prefix="/my/daily-reports", tags=["My Daily Reports"])

# 내 매장: /my/stores (My stores from user_stores)
app_router.include_router(stores_router, prefix="/my/stores", tags=["My Stores"])

# ---------------------------------------------------------------------------
# Phase 10 라우터 등록 — Register Phase 10 (Voices) routers
# ---------------------------------------------------------------------------
app_router.include_router(voices_router, prefix="/my/voices", tags=["My Voices"])

# ---------------------------------------------------------------------------
# Storage 라우터 등록 — Register Storage router
# ---------------------------------------------------------------------------
app_router.include_router(storage_router, prefix="/storage", tags=["App Storage"])

# ---------------------------------------------------------------------------
# Schedule System 라우터 등록 — Work Roles + Request Templates + Schedule Requests
# ---------------------------------------------------------------------------
app_router.include_router(app_work_roles_router, prefix="/my", tags=["My Work Roles"])
app_router.include_router(request_templates_router, prefix="/my", tags=["My Schedule Templates"])
app_router.include_router(schedule_requests_router, prefix="/my", tags=["My Schedule Requests"])
app_router.include_router(schedule_entries_router, prefix="/my", tags=["My Schedules"])

# ---------------------------------------------------------------------------
# Inventory 라우터 등록 — Register Inventory routers
# ---------------------------------------------------------------------------
from app.api.app.inventory import router as app_inventory_router
app_router.include_router(app_inventory_router, tags=["App Inventory"])
