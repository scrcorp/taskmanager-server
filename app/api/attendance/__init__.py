"""Attendance device 라우터 묶음 — `/api/v1/attendance` 하위로 mount.

매장 공용 기기 self-service API. JWT 가 아닌 device token 기반 인증.
책임별로 파일 분리:
    - device:      /register, /me, /store, /stores, DELETE /me
    - clock:       /clock-in, /clock-out, /break-start, /break-end
    - identify:    /identify-by-pin  (Phase 3 — PIN 단독 식별)
    - dashboard:   /today-staff, /notices
    - admin:       /admin/* (관리자 모드 — schedule 편집, attendance 상태 변경 등)
    - tip:         /tip-entry, /tip-entry/eligible-receivers
    - app_version: /app-version
"""

from fastapi import APIRouter

from . import admin, app_version, clock, dashboard, device, identify, tip


router: APIRouter = APIRouter()
router.include_router(device.router)
router.include_router(clock.router)
router.include_router(identify.router)
router.include_router(dashboard.router)
router.include_router(admin.router)
router.include_router(tip.router)
router.include_router(app_version.router)
