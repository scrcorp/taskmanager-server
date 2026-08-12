"""org 접근 게이트 — 이 org 에서 지금 무엇이 막혔는지.

`app/services/access_service.py` 의 `REASON_*` 상수와 **같은 문자열**이다.
콘솔이 이 코드로 화면을 갈라 렌더한다(`/license-inactive` 전용 페이지).
따라서 개명하면 콘솔이 조용히 일반 403 화면으로 퇴화한다(X2).
"""

from __future__ import annotations

from app.core.error_codes._registry import domain

ACCESS = domain("access")

_CLIENTS = (
    "console/src/lib/api.ts",
    "console/src/types/index.ts",
    "console/src/app/license-inactive/page.tsx",
)

NOT_A_MEMBER = ACCESS.legacy(
    "NOT_A_MEMBER",
    403,
    "You are not a member of this organization.",
    frozen=True,
    clients=_CLIENTS,
)

ORG_LICENSE_INACTIVE = ACCESS.legacy(
    "ORG_LICENSE_INACTIVE",
    403,
    "This organization's license is inactive.",
    hint="Contact your administrator.",
    frozen=True,
    clients=_CLIENTS,
)

ORG_ACCESS_REVOKED = ACCESS.legacy(
    "ORG_ACCESS_REVOKED",
    403,
    "Your access to this organization has been revoked.",
    frozen=True,
    clients=_CLIENTS,
)
