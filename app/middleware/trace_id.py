"""요청마다 trace_id 를 발급하고 응답 헤더 `X-Request-Id` 로 노출한다.

왜 전용 미들웨어인가 (E4)
-------------------------
Axiom 미들웨어 안에서 만들면 `settings.DEBUG` 일 때 Axiom 이 **완전 패스스루**라
로컬·워크트리에서 trace_id 가 아예 생기지 않는다. 이 트랙의 발단이 "개발 중 500 의
원인을 못 찾은 것"인데 개발 환경에서 안 되면 의미가 없다.

왜 순수 ASGI 인가
-----------------
`BaseHTTPMiddleware` 는 다운스트림을 **별도 task** 로 돌린다. ContextVar 를 그 안에서
설정하면 바깥 프레임으로 전파되지 않는데, 500 을 처리하는 Starlette
`ServerErrorMiddleware` 는 모든 user middleware 보다 **바깥**에 있다. 순수 ASGI 로
같은 task 에서 ContextVar 를 먼저 심어 두면 500 핸들러도 같은 값을 읽는다.
(= 정작 가장 필요한 500 에서 trace_id 가 빠지는 사고를 막는다)

헤더 vs 봉투
------------
헤더 `X-Request-Id` 는 **부가 수단**이다. 500 은 보통
`UncaughtExceptionMiddleware`(가장 안쪽)가 응답으로 바꾸므로 여기 send 래퍼를 타지만,
그 층보다 바깥에서 터진 예외는 ServerErrorMiddleware 가 이 미들웨어 **바깥**에서
응답을 만들어 래퍼를 건너뛴다 — 그래서 각 예외 핸들러가 헤더를 직접 달고,
봉투 본문의 `error.trace_id` 가 정본이다.
브라우저 JS 가 헤더를 읽으려면 CORS `expose_headers` 에 등록돼 있어야 한다(main.py).
"""

from __future__ import annotations

from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.error_envelope import REQUEST_ID_HEADER, new_trace_id, set_trace_id

_HEADER_BYTES = REQUEST_ID_HEADER.lower().encode("latin-1")


class TraceIdMiddleware:
    """요청 시작 시 trace_id 를 ContextVar 에 심고, 응답 헤더에 붙인다."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # 클라이언트가 보낸 X-Request-Id 는 신뢰하지 않는다 — 로그 키가 외부 입력으로
        # 오염되면 상관관계 자체가 무의미해진다. 분산 추적이 필요해지면 별도 필드로.
        trace_id = new_trace_id()
        set_trace_id(trace_id)
        scope.setdefault("state", {})["trace_id"] = trace_id

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers") or [])
                if not any(k.lower() == _HEADER_BYTES for k, _ in headers):
                    headers.append((_HEADER_BYTES, trace_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


def trace_id_of(scope_or_request: Any) -> str | None:
    """디버깅 편의 — request.state 에 심어둔 값 조회."""
    state = getattr(scope_or_request, "state", None)
    return getattr(state, "trace_id", None) if state is not None else None
