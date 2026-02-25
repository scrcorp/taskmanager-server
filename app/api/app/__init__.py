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
from app.api.app.assignments import router as assignments_router
from app.api.app.checklist_instances import router as checklist_instances_router

# Phase 3 — Communication 라우터 임포트
from app.api.app.announcements import router as announcements_router
from app.api.app.tasks import router as tasks_router
from app.api.app.notifications import router as notifications_router

# Phase 5 — Attendance 라우터 임포트
from app.api.app.attendances import router as attendance_router

# Phase 10 — Issue Reports 라우터 임포트
from app.api.app.issue_reports import router as issue_reports_router

app_router: APIRouter = APIRouter()

# ---------------------------------------------------------------------------
# Phase 1 라우터 등록 — Register Phase 1 (Foundation) routers
# ---------------------------------------------------------------------------
app_router.include_router(auth_router, prefix="/auth", tags=["App Auth"])
# 프로필: /profile 엔드포인트 (GET/PUT my profile)
app_router.include_router(profile_router, tags=["App Profile"])

# ---------------------------------------------------------------------------
# Phase 2 라우터 등록 — Register Phase 2 (Core Workflow) routers
# ---------------------------------------------------------------------------
# 내 업무 배정: /my/work-assignments 하위 (My work assignments)
app_router.include_router(assignments_router, prefix="/my", tags=["My Assignments"])
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
# Phase 10 라우터 등록 — Register Phase 10 (Issue Reports) routers
# ---------------------------------------------------------------------------
app_router.include_router(issue_reports_router, prefix="/my/issue-reports", tags=["My Issue Reports"])
