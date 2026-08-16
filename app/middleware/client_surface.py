"""요청마다 클라이언트 표면(채널) contextvar 를 심는다 — 감사 기록용.

trace_id 미들웨어와 같은 이유로 **순수 ASGI** 다: `BaseHTTPMiddleware` 는 다운스트림을
별도 task 로 돌려 ContextVar 가 요청 처리 프레임에 전파되지 않을 수 있다.
판정 로직 자체는 app/core/client_surface.resolve_channel 이 단일 출처.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.client_surface import resolve_channel, set_current_channel

_HEADER = b"x-client-surface"


class ClientSurfaceMiddleware:
    """경로 prefix + `X-Client-Surface` 헤더(허용 목록 내)로 채널을 결정해 심는다."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        header_value: str | None = None
        for k, v in scope.get("headers") or []:
            if k.lower() == _HEADER:
                header_value = v.decode("latin-1", errors="replace")
                break
        set_current_channel(resolve_channel(scope.get("path", ""), header_value))
        try:
            await self.app(scope, receive, send)
        finally:
            # 서버가 task 를 재사용해도 이전 요청의 채널이 새 요청/백그라운드로 새지 않게
            set_current_channel(None)
