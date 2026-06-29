"""Backoffice — 플랫폼 운영자 전용 평면 (org 권한 시스템 밖).

Backoffice — vendor-internal operator surface, OUTSIDE the org RBAC.
A separate auth plane (ENV single credential + signed session cookie) that
transcends organizations. Mounted at a secret URL slug in main.py only when
`settings.backoffice_enabled`.

SoT: docs/99_inbox/2026-06-24 HTM control-plane 운영자콘솔 + EMPID 임포트 설계.md
"""

from app.api.backoffice.routes import router as backoffice_router
from app.api.backoffice.tools.empid import router as _empid_router

# 도구 라우터를 backoffice 라우터에 장착 (비밀경로 prefix 하위)
backoffice_router.include_router(_empid_router)

__all__ = ["backoffice_router"]
