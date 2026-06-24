"""Control Plane — 플랫폼 운영자 전용 평면 (org 권한 시스템 밖).

Control Plane — vendor-internal operator surface, OUTSIDE the org RBAC.
A separate auth plane (ENV single credential + signed session cookie) that
transcends organizations. Mounted at a secret URL slug in main.py only when
`settings.control_plane_enabled`.

SoT: docs/99_inbox/2026-06-24 HTM control-plane 운영자콘솔 + EMPID 임포트 설계.md
"""

from app.api.control.routes import router as control_router

__all__ = ["control_router"]
