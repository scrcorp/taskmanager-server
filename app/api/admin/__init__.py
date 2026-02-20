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
    - announcements: 공지사항 관리 (Announcement management)
    - tasks: 추가 업무 관리 (Additional task management)
    - notifications: 관리자 알림 관리 (Admin notification management)
"""

from fastapi import APIRouter

# Phase 1 — Foundation 라우터 임포트
from app.api.admin.auth import router as auth_router
from app.api.admin.organizations import router as organizations_router
from app.api.admin.stores import router as stores_router
from app.api.admin.roles import router as roles_router
from app.api.admin.users import router as users_router
from app.api.admin.shifts import router as shifts_router
from app.api.admin.positions import router as positions_router

# Phase 2 — Core Workflow 라우터 임포트
from app.api.admin.checklists import router as checklists_router
from app.api.admin.assignments import router as assignments_router

# Phase 3 — Communication 라우터 임포트
from app.api.admin.announcements import router as announcements_router
from app.api.admin.tasks import router as tasks_router
from app.api.admin.notifications import router as notifications_router

admin_router: APIRouter = APIRouter()

# ---------------------------------------------------------------------------
# Phase 1 라우터 등록 — Register Phase 1 (Foundation) routers
# ---------------------------------------------------------------------------
admin_router.include_router(auth_router, prefix="/auth", tags=["Admin Auth"])
admin_router.include_router(organizations_router, prefix="/organizations", tags=["Organizations"])
admin_router.include_router(stores_router, prefix="/stores", tags=["Stores"])
admin_router.include_router(roles_router, prefix="/roles", tags=["Roles"])
admin_router.include_router(users_router, prefix="/users", tags=["Users"])
admin_router.include_router(shifts_router, tags=["Shifts"])
admin_router.include_router(positions_router, tags=["Positions"])

# ---------------------------------------------------------------------------
# Phase 2 라우터 등록 — Register Phase 2 (Core Workflow) routers
# ---------------------------------------------------------------------------
# 체크리스트: /stores/{store_id}/checklist-templates 형태 (nested under stores)
admin_router.include_router(checklists_router, tags=["Checklists"])
# 업무 배정: /work-assignments 하위 (Work assignments)
admin_router.include_router(assignments_router, prefix="/work-assignments", tags=["Assignments"])

# ---------------------------------------------------------------------------
# Phase 3 라우터 등록 — Register Phase 3 (Communication) routers
# ---------------------------------------------------------------------------
admin_router.include_router(announcements_router, prefix="/announcements", tags=["Announcements"])
admin_router.include_router(tasks_router, prefix="/additional-tasks", tags=["Additional Tasks"])
admin_router.include_router(notifications_router, prefix="/notifications", tags=["Admin Notifications"])
