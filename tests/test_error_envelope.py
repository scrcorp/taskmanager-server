"""에러 봉투(envelope) 통합 테스트 — 출구 한 곳이 모든 모양을 흡수하는지 검증.

계약: `docs/99_inbox/2026-08-11 에러 처리 일원화 - 봉투 계약안.md` (E1 = 후보 B)

여기서 지키는 것 중 **가장 중요한 것은 `detail` 무변경**이다.
구버전 HTMA(사이드로드 APK)는 `detail` 만 읽고, 되돌릴 수단이 서버 롤백뿐이다.
`detail` 이 조금이라도 변형되면 조기 clock-in / 스케줄 겹침 확인이 **기능 정지**한다.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.core import error_codes, error_envelope
from app.core.error_envelope import (
    REQUEST_ID_HEADER,
    current_trace_id,
    new_trace_id,
    register_error_handlers,
)
from app.middleware.trace_id import TraceIdMiddleware
from app.middleware.uncaught_exception import UncaughtExceptionMiddleware
from app.utils.exceptions import (
    BadRequestError,
    CaptureTimeRequiredError,
    ConflictError,
    DuplicateError,
)


# ---------------------------------------------------------------------------
# 실제 앱과 같은 배선을 가진 최소 앱.
# 500·ValueError 를 일으키는 라우트를 프로덕션 앱에 남기지 않기 위해 격리한다.
# ---------------------------------------------------------------------------


class _Body(BaseModel):
    n: int


# CORS 설정에 들어 있는 오리진 / 들어 있지 않은 오리진.
# 500 에 CORS 헤더를 붙이더라도 **설정을 우회하면 안 된다** — 두 값으로 확인한다.
ALLOWED_ORIGIN = "http://localhost:3000"
FOREIGN_ORIGIN = "http://evil.example"


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/str400")
    async def _str400() -> None:
        raise BadRequestError("Break end must be after break start.")

    @app.get("/dup")
    async def _dup() -> None:
        raise DuplicateError("Username already exists")

    @app.get("/conflict")
    async def _conflict() -> None:
        raise ConflictError("Email already registered", email="a@b.c")

    @app.get("/domain-flat")
    async def _domain_flat() -> None:
        # 조기 clock-in — 구버전이 detail 최상위에서 minutes_early 를 읽는다.
        raise HTTPException(
            status_code=400,
            detail={
                "code": "early_clock_in_reason_required",
                "minutes_early": 23,
                "schedule_id": "s-1",
                "message": "Clocking in early requires a reason.",
            },
        )

    @app.get("/schedule409")
    async def _schedule409() -> None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SCHEDULE_WARNINGS_UNCONFIRMED",
                "message": "This employee already has an overlapping schedule.",
                "warnings": [{"code": "OVERLAPPING_SCHEDULE", "params": {"user_id": "u1"}}],
                "retry": {"force": True},
            },
        )

    @app.get("/code-only")
    async def _code_only() -> None:
        raise HTTPException(status_code=409, detail={"code": "provisional_candidate_exists"})

    @app.get("/code-only-unregistered")
    async def _code_only_unregistered() -> None:
        # 레지스트리에 없는 코드. 문구를 **지어내면 안 된다**.
        raise HTTPException(status_code=409, detail={"code": "not_in_the_registry"})

    @app.get("/app-error")
    async def _app_error() -> None:
        raise CaptureTimeRequiredError()

    @app.get("/needs-auth")
    async def _needs_auth() -> None:
        raise HTTPException(
            status_code=401,
            detail="Session expired",
            headers={"WWW-Authenticate": 'Bearer realm="api"'},
        )

    @app.get("/bad-uuid")
    async def _bad_uuid() -> None:
        uuid.UUID("not-a-uuid")

    @app.get("/pydantic-bug")
    async def _pydantic_bug() -> None:
        # 응답 모델 직렬화 실패 등 **서버 버그**. 422 로 둔갑하면 안 된다.
        _Body(n="not-an-int")  # type: ignore[arg-type]

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("kaboom")

    @app.get("/items/{n}")
    async def _items(n: int) -> dict[str, int]:
        return {"n": n}

    @app.get("/api/v1/thing")
    async def _api_thing() -> None:
        raise BadRequestError("api plane")

    # 실제 앱과 **같은 순서**로 감싼다 — 이 순서 자체가 검증 대상이다.
    # `add_middleware` 는 insert(0, ...) 라서 나중에 등록한 것이 바깥이므로
    # 바깥→안쪽 = TraceId → CORS → UncaughtException → 라우터.
    # UncaughtException 이 CORS 안쪽이어야 500 응답에도 CORS 헤더가 붙는다.
    app.add_middleware(UncaughtExceptionMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ALLOWED_ORIGIN],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER],
    )
    app.add_middleware(TraceIdMiddleware)
    register_error_handlers(app)
    return app


@pytest.fixture(scope="module")
def envelope_app() -> FastAPI:
    return _build_app()


@pytest.fixture
async def envelope_client(envelope_app: FastAPI):
    # raise_app_exceptions=False — Starlette ServerErrorMiddleware 는 500 응답을 보낸 뒤
    # 예외를 재발생시킨다(uvicorn 로그 유지). 테스트에서는 응답 자체를 검사해야 한다.
    transport = ASGITransport(app=envelope_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# trace_id 자체
# ---------------------------------------------------------------------------


def test_trace_id_format() -> None:
    """사람이 화면을 보고 옮겨 적는 것을 전제 — 10자, 혼동 문자(I/L/O/U) 없음."""
    value = new_trace_id()
    assert len(value) == 10
    assert set(value) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    assert new_trace_id() != value


def test_trace_id_is_time_sortable() -> None:
    """앞 8자는 시간 성분 — 로그를 시간순으로 정렬/범위 검색할 수 있어야 한다.

    (뒤 2자에는 난수 9비트가 섞이므로 같은 ms 안에서는 순서가 보장되지 않는다)
    """
    import time as _time

    first = new_trace_id()
    _time.sleep(0.05)
    second = new_trace_id()
    assert second[:8] > first[:8]


def test_current_trace_id_outside_request() -> None:
    """미들웨어 밖(백그라운드 작업)에서도 None 이 아니라 즉석 발급된다."""
    assert len(current_trace_id()) == 10


# ---------------------------------------------------------------------------
# 문자열 detail (666곳) — detail 무변경 + status 기반 코드
# ---------------------------------------------------------------------------


async def test_string_detail_stays_a_string(envelope_client: AsyncClient) -> None:
    resp = await envelope_client.get("/str400")
    body = resp.json()
    assert resp.status_code == 400
    # ⚠️ 구버전 HTMA 는 detail 이 String 일 때만 읽는다. 이 한 줄이 666곳을 살린다.
    assert body["detail"] == "Break end must be after break start."
    assert isinstance(body["detail"], str)
    err = body["error"]
    assert err["code"] == "BAD_REQUEST"
    assert err["code_source"] == "status"
    assert err["message"] == "Break end must be after break start."
    assert err["hint"] is None
    assert err["params"] == {}
    assert len(err["trace_id"]) == 10


async def test_class_based_codes_split_409(envelope_client: AsyncClient) -> None:
    """같은 409 라도 중복(DUPLICATE)과 상태충돌(CONFLICT)은 다른 코드다."""
    dup = (await envelope_client.get("/dup")).json()
    assert dup["error"]["code"] == "DUPLICATE"
    conflict = (await envelope_client.get("/conflict")).json()
    assert conflict["error"]["code"] == "CONFLICT"
    # ConflictError 는 detail 을 {"message": ..., **kwargs} dict 로 만든다 — 원형 유지.
    assert conflict["detail"] == {"message": "Email already registered", "email": "a@b.c"}
    assert conflict["error"]["params"] == {"email": "a@b.c"}


async def test_no_message_inference_from_text(envelope_client: AsyncClient) -> None:
    """문구를 보고 코드를 추론하지 않는다(X4) — 401 문구가 'Session expired' 여도
    코드는 status 기반 UNAUTHORIZED 다."""
    resp = await envelope_client.get("/needs-auth")
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"
    assert resp.json()["error"]["code_source"] == "status"


# ---------------------------------------------------------------------------
# dict detail (132곳) — 원형 보존 + error 안에서만 정규화
# ---------------------------------------------------------------------------


async def test_flat_dict_detail_is_preserved(envelope_client: AsyncClient) -> None:
    """X3 — 부가필드를 params 로 감싸면 구버전이 '0m early' 오표시를 한다."""
    body = (await envelope_client.get("/domain-flat")).json()
    assert body["detail"] == {
        "code": "early_clock_in_reason_required",
        "minutes_early": 23,
        "schedule_id": "s-1",
        "message": "Clocking in early requires a reason.",
    }
    err = body["error"]
    assert err["code"] == "early_clock_in_reason_required"  # X2 — lower_snake 개명 금지
    assert err["code_source"] == "domain"
    assert err["message"] == "Clocking in early requires a reason."
    # error 쪽에서만 미지 키를 params 로 내린다.
    assert err["params"] == {"minutes_early": 23, "schedule_id": "s-1"}


async def test_whitelist_keys_stay_top_level(envelope_client: AsyncClient) -> None:
    """errors/warnings/retry/hint 는 error 안에서도 최상위 (E1-b 화이트리스트)."""
    body = (await envelope_client.get("/schedule409")).json()
    err = body["error"]
    assert err["code"] == "SCHEDULE_WARNINGS_UNCONFIRMED"
    assert err["retry"] == {"force": True}
    assert err["warnings"] == [
        {"code": "OVERLAPPING_SCHEDULE", "params": {"user_id": "u1"}}
    ]
    assert err["params"] == {}
    # 겹침 확인 모달은 detail 을 읽는다 — 원형 유지 확인.
    assert body["detail"]["retry"] == {"force": True}


async def test_code_only_detail_uses_registry_message(
    envelope_client: AsyncClient,
) -> None:
    """message 없는 59곳 — 레지스트리 문구를 쓴다(E1-d 목표 상태).

    이게 없으면 HTMA **고객용 키오스크 화면**에 `provisional_candidate_exists` 같은
    lower_snake 원문이 그대로 뜬다.
    """
    body = (await envelope_client.get("/code-only")).json()
    # detail 은 바이트 동일 — 레지스트리 조회는 error.message 에만 영향(X1).
    assert body["detail"] == {"code": "provisional_candidate_exists"}
    err = body["error"]
    assert err["code"] == "provisional_candidate_exists"  # X2 — 개명 금지
    assert err["code_source"] == "domain"
    assert err["message"] == error_codes.get("provisional_candidate_exists").message
    assert err["message"] != err["code"]
    assert "_" not in err["message"]  # lower_snake 원문이 새어 나오지 않는다


async def test_unregistered_code_keeps_raw_code_as_message(
    envelope_client: AsyncClient,
) -> None:
    """레지스트리에 없으면 **문구를 지어내지 않는다** — 코드 원문 그대로.

    그럴듯한 일반 문구("Bad request.")를 깔면 아무도 그 지점을 고치지 않는다(E1-d).
    """
    assert error_codes.get("not_in_the_registry") is None
    body = (await envelope_client.get("/code-only-unregistered")).json()
    assert body["detail"] == {"code": "not_in_the_registry"}
    assert body["error"]["message"] == "not_in_the_registry"
    assert body["error"]["code_source"] == "domain"


async def test_app_error_hint_is_top_level(envelope_client: AsyncClient) -> None:
    body = (await envelope_client.get("/app-error")).json()
    err = body["error"]
    assert err["code"] == "CAPTURE_TIME_REQUIRED"
    assert err["code_source"] == "domain"
    assert err["hint"].startswith("Retake it")
    assert err["params"] == {}
    assert body["detail"]["hint"] == err["hint"]


# ---------------------------------------------------------------------------
# X6 — exc.headers 전달 (401 재인증 11곳)
# ---------------------------------------------------------------------------


async def test_exception_headers_survive(envelope_client: AsyncClient) -> None:
    resp = await envelope_client.get("/needs-auth")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == 'Bearer realm="api"'
    assert resp.headers[REQUEST_ID_HEADER] == resp.json()["error"]["trace_id"]


# ---------------------------------------------------------------------------
# 422 / ValueError
# ---------------------------------------------------------------------------


async def test_request_validation_keeps_raw_array(envelope_client: AsyncClient) -> None:
    resp = await envelope_client.get("/items/abc")
    body = resp.json()
    assert resp.status_code == 422
    # FastAPI 원형 배열 유지 — 기존 소비자/테스트가 이 모양을 읽는다.
    assert isinstance(body["detail"], list)
    assert body["detail"][0]["loc"] == ["path", "n"]
    err = body["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["code_source"] == "status"
    assert err["params"]["fields"][0]["field"] == "n"
    assert err["params"]["fields"][0]["reason"] == "int_parsing"


async def test_value_error_becomes_422(envelope_client: AsyncClient) -> None:
    """수동 UUID 파싱 69곳이 500 이 아니라 422 가 된다(E6-b)."""
    resp = await envelope_client.get("/bad-uuid")
    body = resp.json()
    assert resp.status_code == 422
    assert body["error"]["code"] == "INVALID_PARAMETER"
    assert isinstance(body["detail"], str)  # 구버전 파서가 읽을 수 있어야 한다
    assert body["detail"] == body["error"]["message"]


async def test_pydantic_validation_error_is_500_not_422(
    envelope_client: AsyncClient,
) -> None:
    """pydantic ValidationError 는 ValueError 의 하위 클래스다.

    서버 버그가 422 로 둔갑하면 원인을 영영 못 찾으므로 500 경로로 보낸다.
    """
    resp = await envelope_client.get("/pydantic-bug")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# 500 — 이 트랙의 발단
# ---------------------------------------------------------------------------


async def test_unhandled_exception_is_json_envelope(
    envelope_client: AsyncClient,
) -> None:
    """지금은 text/plain 'Internal Server Error' 라 JSON 파싱 자체가 실패한다."""
    resp = await envelope_client.get("/boom")
    assert resp.status_code == 500
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["detail"] == "Something went wrong on our side."
    assert isinstance(body["detail"], str)
    err = body["error"]
    assert err["code"] == "INTERNAL_ERROR"
    assert err["code_source"] == "status"
    assert err["hint"]
    assert len(err["trace_id"]) == 10


async def test_500_carries_trace_id_in_body_and_header(
    envelope_client: AsyncClient,
) -> None:
    """ServerErrorMiddleware 는 모든 user middleware 보다 **바깥**이다.

    request.state 에만 의존했다면 정작 가장 필요한 500 에서 trace_id 가 빠진다.
    ContextVar 가 실제로 바깥 프레임까지 전달되는지의 실측 테스트.
    """
    resp = await envelope_client.get("/boom")
    trace_id = resp.json()["error"]["trace_id"]
    assert resp.headers[REQUEST_ID_HEADER] == trace_id


async def test_500_logs_traceback_with_trace_id(
    envelope_client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Axiom 은 traceback 을 안 남기고 DEBUG 면 아예 비활성이다 —
    핸들러의 logger.exception 이 로컬에서 유일한 traceback 출처다."""
    with caplog.at_level("ERROR", logger="uvicorn.error"):
        resp = await envelope_client.get("/boom")
    trace_id = resp.json()["error"]["trace_id"]
    record = next(r for r in caplog.records if r.name == "uvicorn.error")
    assert trace_id in record.getMessage()
    assert record.exc_info is not None
    assert "RuntimeError" in caplog.text


# ---------------------------------------------------------------------------
# 500 + CORS — 브라우저가 봉투를 **읽을 수 있어야** 이 트랙의 목표가 달성된다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/str400", "/boom"])
async def test_cors_headers_present_on_errors_including_500(
    envelope_client: AsyncClient, path: str
) -> None:
    """500 에도 4xx 와 **동일하게** CORS 3종이 붙는다.

    회귀 방지 대상: Starlette `ServerErrorMiddleware` 는 모든 user middleware 바깥이라
    거기서 만든 500 은 CORS 를 건너뛴다. 그러면 브라우저가 응답을 JS 에 넘기지 않아
    콘솔 사용자는 500 봉투도 trace_id 도 **읽지 못한다**.
    """
    resp = await envelope_client.get(path, headers={"Origin": ALLOWED_ORIGIN})
    assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert resp.headers["access-control-allow-credentials"] == "true"
    assert resp.headers["access-control-expose-headers"] == REQUEST_ID_HEADER
    # 헤더를 읽을 수 있게 되었으니 봉투의 trace_id 와 실제로 일치해야 의미가 있다.
    assert resp.headers[REQUEST_ID_HEADER] == resp.json()["error"]["trace_id"]


@pytest.mark.parametrize("path", ["/str400", "/boom"])
async def test_cors_config_is_not_bypassed_for_foreign_origin(
    envelope_client: AsyncClient, path: str
) -> None:
    """설정에 없는 Origin 에는 `allow-origin` 을 붙이지 않는다.

    500 에 헤더를 손으로 덧붙이는 방식이었다면 여기서 무너진다 — 헤더 생성은 반드시
    `CORSMiddleware` 한 곳에서만 일어나야 한다.
    """
    resp = await envelope_client.get(path, headers={"Origin": FOREIGN_ORIGIN})
    assert "access-control-allow-origin" not in resp.headers


async def test_each_request_gets_a_new_trace_id(envelope_client: AsyncClient) -> None:
    first = (await envelope_client.get("/str400")).json()["error"]["trace_id"]
    second = (await envelope_client.get("/str400")).json()["error"]["trace_id"]
    assert first != second


# ---------------------------------------------------------------------------
# X5 — HTML 평면을 JSON 으로 깨지 않는다
# ---------------------------------------------------------------------------


async def test_html_plane_stays_html(envelope_client: AsyncClient) -> None:
    resp = await envelope_client.get("/nope", headers={"Accept": "text/html"})
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "NOT_FOUND" in resp.text
    assert resp.headers[REQUEST_ID_HEADER] in resp.text


async def test_api_path_is_json_even_for_browser_accept(
    envelope_client: AsyncClient,
) -> None:
    """/api/ 아래는 Accept 가 text/html 이어도 항상 JSON — 클라 파서가 깨지면 안 된다."""
    resp = await envelope_client.get("/api/v1/thing", headers={"Accept": "text/html"})
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["detail"] == "api plane"


# ---------------------------------------------------------------------------
# 실제 앱(app.main) 배선 확인 — 위 테스트는 격리 앱이므로 별도로 확인한다.
# ---------------------------------------------------------------------------


async def test_real_app_404_is_enveloped(async_client: AsyncClient) -> None:
    resp = await async_client.get("/api/v1/console/definitely-not-a-route")
    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"] == "Not Found"  # FastAPI 원형 문자열 유지
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["code_source"] == "status"
    assert resp.headers[REQUEST_ID_HEADER] == body["error"]["trace_id"]


async def test_real_app_401_is_enveloped(async_client: AsyncClient) -> None:
    resp = await async_client.get("/api/v1/console/stores")
    assert resp.status_code in (401, 403)
    body = resp.json()
    assert "detail" in body and "error" in body
    assert body["error"]["code"] in ("UNAUTHORIZED", "FORBIDDEN")
    assert len(body["error"]["trace_id"]) == 10


async def test_real_app_health_has_request_id_header(async_client: AsyncClient) -> None:
    """정상 응답에도 헤더가 붙는다 — 성공 요청도 로그와 이어져야 한다."""
    resp = await async_client.get("/health")
    assert resp.status_code == 200
    assert len(resp.headers[REQUEST_ID_HEADER]) == 10


def test_real_app_uncaught_handler_is_inside_cors() -> None:
    """실제 앱에서도 미포착 예외 층이 **CORS 안쪽**인지 확인한다.

    격리 앱 테스트만으로는 main.py 의 `add_middleware` 순서가 바뀌어도 못 잡는다.
    `user_middleware` 는 바깥→안쪽 순이므로 CORS 의 인덱스가 더 작아야 한다.
    """
    from app.main import app as real_app

    order = [m.cls.__name__ for m in real_app.user_middleware]
    assert order.index("CORSMiddleware") < order.index("UncaughtExceptionMiddleware")


# ---------------------------------------------------------------------------
# Axiom 상관관계 — 화면의 id 로 로그를 짚는 것이 이 트랙의 핵심 가치다.
# ---------------------------------------------------------------------------


class _StubAxiomClient:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def ingest_events(self, dataset: str, events: list[dict]) -> None:
        self.events.extend(events)


async def test_axiom_log_shares_trace_id_and_preserves_headers() -> None:
    """Axiom 은 4xx/5xx 응답 body 를 소비한 뒤 **재포장**한다.

    그 과정에서 헤더가 유실되면 401 재인증(WWW-Authenticate)과 X-Request-Id 가
    조용히 죽는다. 실제로 보존되는지 확인한다. 동시에 log_event 의 trace_id 가
    응답 봉투의 trace_id 와 **같은 값**인지(= 로그를 짚을 수 있는지) 확인한다.

    ⚠️ 여기서 앱을 새로 만드는 이유 — TraceIdMiddleware 를 두 겹으로 쌓으면
    안쪽이 새 id 를 발급해 바깥과 값이 갈린다. 실제 배선은 한 겹이다.
    """
    from app.middleware.axiom_logging import AxiomLoggingMiddleware

    inner = FastAPI()

    @inner.get("/needs-auth")
    async def _needs_auth() -> None:
        raise HTTPException(
            status_code=401,
            detail="Session expired",
            headers={"WWW-Authenticate": 'Bearer realm="api"'},
        )

    register_error_handlers(inner)
    axiom = AxiomLoggingMiddleware(inner)
    stub = _StubAxiomClient()
    axiom._client = stub  # type: ignore[assignment]
    stack = TraceIdMiddleware(axiom)

    transport = ASGITransport(app=stack, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/needs-auth", headers={"X-App-Version": "1.0.17+38"}
        )

    body = resp.json()
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == 'Bearer realm="api"'  # X6
    assert resp.headers[REQUEST_ID_HEADER] == body["error"]["trace_id"]

    event = stub.events[-1]
    assert event["trace_id"] == body["error"]["trace_id"]
    assert event["error_code"] == "UNAUTHORIZED:status"
    # 버전 텔레메트리 — `detail` 을 지울 수 있는 시점을 판단하는 유일한 근거.
    assert event["app_version"] == "1.0.17+38"


async def test_axiom_logs_device_key_for_attendance_paths(
    envelope_app: FastAPI,
) -> None:
    """기기별 집계용 키 — attendance 경로 + Bearer 토큰일 때만 남는다."""
    import hashlib

    from app.middleware.axiom_logging import AxiomLoggingMiddleware

    app = FastAPI()

    @app.get("/api/v1/attendance/ping")
    async def _ping() -> dict[str, bool]:
        return {"ok": True}

    register_error_handlers(app)
    axiom = AxiomLoggingMiddleware(app)
    stub = _StubAxiomClient()
    axiom._client = stub  # type: ignore[assignment]
    stack = TraceIdMiddleware(axiom)

    transport = ASGITransport(app=stack, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get(
            "/api/v1/attendance/ping",
            headers={"Authorization": "Bearer tok-123", "X-App-Version": "1.0.12+33"},
        )

    event = stub.events[-1]
    # attendance_devices.token_hash 의 앞 12자와 같아야 join 이 된다.
    assert event["device_key"] == hashlib.sha256(b"tok-123").hexdigest()[:12]
    assert event["app_version"] == "1.0.12+33"


# ---------------------------------------------------------------------------
# E7 — 배포된 환경은 예외 메시지를 응답에 싣지 않는다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["production", "prod", "staging", "STAGING"])
def test_deployed_envs_never_expose_exception_text(monkeypatch, env: str) -> None:
    """staging 도 인터넷에 열려 있다 — 내부 예외 메시지가 아무에게나 나가면 안 된다.

    배포된 환경에서 debug 필드는 필요도 없다: trace_id 로 로그를 짚으면
    traceback 전체를 볼 수 있다. 그게 trace_id 를 만든 이유다.
    """
    monkeypatch.setattr(error_envelope.settings, "APP_ENV", env)
    assert error_envelope._is_debug_env() is False


@pytest.mark.parametrize("env", ["local", "development", "dev", "test"])
def test_local_envs_still_expose_exception_text(monkeypatch, env: str) -> None:
    """로컬은 로그를 안 보는 경우가 많아 화면이 유일한 단서다(E7-다)."""
    monkeypatch.setattr(error_envelope.settings, "APP_ENV", env)
    assert error_envelope._is_debug_env() is True
