"""웹 푸시 — 구독/발송에서 클라이언트가 갈라 처리해야 하는 상태.

푸시는 서버에 VAPID 키가 있어야만 동작한다. 키가 없는 환경(로컬에서 키 없이
띄운 경우 등)에서는 "일시적 장애" 가 아니라 **기능이 꺼져 있음** 이므로,
앱이 재시도하지 않고 설정 UI 를 숨기도록 구분 가능한 코드로 내려준다.
"""

from __future__ import annotations

from app.core.error_codes._registry import domain

PUSH = domain("push")

PUSH_NOT_CONFIGURED = PUSH.code(
    "PUSH_NOT_CONFIGURED",
    503,
    "Push notifications are not available right now.",
    hint="This server has no push configuration. Contact your administrator.",
)
