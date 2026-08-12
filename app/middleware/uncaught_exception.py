"""미포착 예외를 **CORS 안쪽에서** 500 봉투로 바꾸는 층.

왜 이 층이 따로 필요한가 (실측으로 드러난 결함)
-----------------------------------------------
`register_error_handlers()` 가 등록한 `Exception` 핸들러는 Starlette 의
`ServerErrorMiddleware` 가 쓴다. 그런데 `ServerErrorMiddleware` 는 **모든 user
middleware 보다 바깥**이라, 거기서 만들어진 500 응답은 `CORSMiddleware` 를
**거치지 않는다.**

실측(2026-08-11, starlette 0.38.6):

    GET /str400  Origin: http://localhost:3000
      → access-control-allow-origin: *
        access-control-expose-headers: X-Request-Id      ← 붙는다

    GET /boom    Origin: http://localhost:3000
      → (CORS 헤더 없음)                                  ← 안 붙는다

브라우저는 CORS 헤더가 없는 교차출처 응답을 **JS 에 넘기지 않는다.** 즉 콘솔 사용자는
500 봉투도, 그 안의 `trace_id` 도 읽을 수 없다. "화면의 id 로 로그를 짚는다"는 이 트랙의
목표가 정작 브라우저에서 미달성이라는 뜻이다.

두 가지 해결책 중 무엇을 골랐나
-------------------------------
(A) `TraceIdMiddleware` 의 send 래퍼에서 5xx 에 CORS 헤더를 보강한다
(B) **CORS 안쪽에 미포착 예외를 잡는 층을 둔다** ← 이것을 골랐다

(A) 는 CORS 헤더를 만드는 로직을 두 곳에 두게 된다. Origin 화이트리스트·credentials·
vary 헤더 규칙을 복제해야 하고, `allow_origins` 설정이 바뀌면 한쪽만 고쳐져 **설정에
없는 Origin 에 헤더가 붙는 보안 사고**가 조용히 생긴다.
(B) 는 CORS 미들웨어에게 **평범한 500 응답**을 넘길 뿐이라, 헤더는 언제나
`CORSMiddleware` 가 자기 설정대로 붙인다 — 설정을 우회할 여지가 원천적으로 없다.

어디에 끼우나 — **가장 안쪽**
-----------------------------
바깥일수록 커버 범위가 넓지만, 안쪽일수록 **바깥 미들웨어 전부가 정상 응답을 본다.**
가장 안쪽(라우터 바로 위)에 두면 CORS 뿐 아니라 Axiom 로깅도 예외 대신 완성된 봉투를
보게 되어 `error.code` / status 가 제대로 집계된다.

대신 Axiom/AppVersion 미들웨어 **자신**이 터지는 경우는 이 층을 벗어난다. 그때는 기존대로
`ServerErrorMiddleware` + 앱의 `Exception` 핸들러가 받는다 — 봉투 본문과 `trace_id` 는
그대로 나가고 CORS 헤더만 빠진다. 미들웨어 자체 버그는 극히 드물고, 그것까지 덮으려고
CORS 바깥으로 올리면 위 (A) 의 문제로 되돌아간다.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.error_envelope import unhandled_exception_handler


class UncaughtExceptionMiddleware:
    """미포착 예외 → 500 봉투 응답. CORS 는 이것을 평범한 응답으로 본다."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # 이미 헤더를 보낸 뒤(스트리밍 중간)라면 새 응답을 만들 수 없다.
            # 그대로 올려보내 ServerErrorMiddleware 가 연결을 끊게 둔다 —
            # 여기서 삼키면 클라가 끊긴 응답을 정상으로 오해한다.
            if response_started:
                raise
            response = await unhandled_exception_handler(Request(scope, receive), exc)
            await response(scope, receive, send)
            # 재-raise 하지 않는다. `unhandled_exception_handler` 가 이미
            # `logger.exception()` 으로 trace_id 와 함께 traceback 을 남기므로,
            # 올려보내면 같은 traceback 이 uvicorn 로그에 한 번 더 찍힐 뿐이다.
