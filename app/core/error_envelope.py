"""에러 봉투(envelope) — **모든 에러 응답이 통과하는 단 하나의 출구**.

계약 (2026-08-11 "에러 처리 일원화 - 봉투 계약안" E1 = 후보 B)
--------------------------------------------------------------
응답 본문은 항상 두 키를 가진다.

    {
      "detail": <원형 그대로>,          # 읽기 전용 레거시 미러
      "error": {                        # 정본(canonical)
        "code": "UPPER_SNAKE",
        "code_source": "domain" | "status",
        "message": "사용자에게 보여줄 원인 한 문장",
        "hint": "다음 행동" | null,
        "params": { ... },
        "trace_id": "01J9F3K2QW"
      }
    }

**방향성 — 서버는 항상 `error` 를 먼저 만들고 `detail` 은 그로부터 파생시킨다.**
`detail` 이 정본인 것처럼 보이는 코드를 새로 쓰지 말 것. 지금 `detail` 이 남아 있는
이유는 오직 하나, **구버전 HTMA(사이드로드 APK)가 `detail` 만 읽기 때문**이다.
모든 기기가 신버전임이 확인되면 `detail` 을 제거해 최종 형태(최상위 `error` 단일)가 된다.

왜 `detail` 을 바이트 동일하게 유지하는가 (절대 어기지 말 것)
------------------------------------------------------------
- 구버전 HTMA `extractApiError` 는 `data['detail'] is String` 일 때만 읽는다.
  문자열 detail 666곳을 dict 로 감싸는 순간 전부 "Failed to load ..." 류 fallback 으로 퇴화한다.
- 구버전이 읽는 **평탄한 부가필드**(`minutes_early` 등)를 `params` 로 한 겹 내리면
  "0m early" 처럼 **틀린 값을 맞는 것처럼** 표시한다 — 정보 소실보다 나쁘다.
- 기능 정지 2건이 실재한다: 조기 clock-in 사유 시트(코드 못 잡으면 제출 불가 → 조기 출근 데드락),
  스케줄 겹침 확인 모달(`detail is! Map` 이면 null → 겹침 저장 불가).
  HTMA 는 사이드로드라 서버 롤백 외에 되돌릴 수단이 없다.

따라서 **정규화(화이트리스트)는 `error` 안에서만** 한다. `detail` 은 손대지 않는다.

코드 부여 규칙
--------------
- dict detail 에 `code` 가 있으면 그것을 쓰고 `code_source="domain"`.
- 없으면 **예외 클래스/status 로만** 일반 코드를 정한다(`code_source="status"`).
  ⚠️ **문구를 보고 코드를 추론하지 않는다.** 이 트랙이 없애려는 문자열 매칭을
  서버 안으로 옮기는 것일 뿐이다.
- `code_source` 는 진척 지표다 — 도메인 코드화가 진행되면 `"status"` 비율이 줄어든다.
  클라 표시기는 `"status"` 일 때 코드→문구 매핑을 시도하지 않고 `message` 를 그대로 띄운다.

문구(`error.message`) 결정 순서
------------------------------
1. `detail` 에 `message` 가 있으면 그것 (원본 의도 우선)
2. 도메인 코드면 **레지스트리 문구**(`error_codes.fallback_message`)
   — `{"code": ...}` 만 던지는 59곳이 여기 해당한다. 이게 없으면
   `"username_taken"` 같은 lower_snake 원문이 **고객이 보는 화면**에 그대로 뜬다.
3. 레지스트리에도 없으면 코드 원문 (문구를 **지어내지 않는다**)
4. status 기반이면 status 일반 문구

`detail` 은 이 결정에 **전혀 영향받지 않는다** — 바이트 동일 유지(X1).
"""

from __future__ import annotations

import secrets
import time
from contextvars import ContextVar
from typing import Any

from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core import error_codes
from app.utils.exceptions import (
    BadRequestError,
    ConflictError,
    DuplicateError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)

# ---------------------------------------------------------------------------
# trace_id
# ---------------------------------------------------------------------------
# 이름 주의 — **`request_id` 를 쓰지 않는다.**
# 서버에는 이미 `schedules.request_id`(스케줄 변경요청 FK)가 있어 같은 응답 JSON 안에서
# 두 의미가 충돌한다. 봉투 필드는 `trace_id`, HTTP 헤더는 업계 관례인 `X-Request-Id`.

REQUEST_ID_HEADER = "X-Request-Id"

# Crockford base32 — I/L/O/U 제외. 매장 직원이 화면을 보고 슬랙에 타이핑하는 경로가
# 실제이므로 혼동 문자를 뺀다. UUID4 전체(36자)는 옮겨 적기에 비현실적이다.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# ContextVar 인 이유 — Starlette 의 ServerErrorMiddleware(500 처리)는 **모든 user
# middleware 보다 바깥**이라, 미들웨어가 `request.state` 에 심은 값에 의존하면
# 정작 가장 필요한 500 에서 trace_id 가 빠질 수 있다. ContextVar 는 같은 task 안이라
# 바깥 프레임에서도 읽힌다.
_TRACE_ID: ContextVar[str | None] = ContextVar("htm_trace_id", default=None)


def new_trace_id() -> str:
    """시간 정렬 가능한 10자 식별자. 상위 41비트=ms epoch, 하위 9비트=난수."""
    value = (int(time.time() * 1000) << 9) | secrets.randbits(9)
    chars = []
    for shift in range(45, -1, -5):  # 50비트 → 5비트 × 10자
        chars.append(_ALPHABET[(value >> shift) & 0x1F])
    return "".join(chars)


def set_trace_id(value: str) -> None:
    _TRACE_ID.set(value)


def current_trace_id() -> str:
    """현재 요청의 trace_id. 미들웨어 밖(백그라운드 작업 등)이면 즉석 발급."""
    value = _TRACE_ID.get()
    if value is None:
        value = new_trace_id()
        _TRACE_ID.set(value)
    return value


# ---------------------------------------------------------------------------
# status / 예외 클래스 → 일반 코드
# ---------------------------------------------------------------------------

# 문자열 detail 666곳이 받는 코드. **문구가 아니라 타입/status 로만** 정한다.
_STATUS_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    402: "PAYMENT_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    410: "GONE",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_ERROR",
    426: "UPGRADE_REQUIRED",
    429: "TOO_MANY_REQUESTS",
    500: "INTERNAL_ERROR",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
    504: "GATEWAY_TIMEOUT",
}

# 같은 status 라도 클래스가 다르면 의미가 다른 경우만 여기서 갈라준다.
# (409 는 DuplicateError=중복 vs ConflictError=상태충돌 두 뜻이 섞여 있다)
_CLASS_CODES: list[tuple[type, str]] = [
    (DuplicateError, "DUPLICATE"),
    (ConflictError, "CONFLICT"),
    (NotFoundError, "NOT_FOUND"),
    (ForbiddenError, "FORBIDDEN"),
    (UnauthorizedError, "UNAUTHORIZED"),
    (BadRequestError, "BAD_REQUEST"),
]

# status 기반 코드일 때, 보여줄 문장이 아예 없을 때만 쓰는 최후의 문구.
_STATUS_MESSAGES: dict[int, str] = {
    400: "The request could not be processed.",
    401: "Please sign in again.",
    403: "You do not have permission to do this.",
    404: "The requested item was not found.",
    405: "This action is not allowed here.",
    409: "This conflicts with existing data.",
    410: "This is no longer available.",
    413: "The file is too large.",
    415: "This file type is not supported.",
    422: "Some fields are invalid. Please review your input.",
    429: "Too many requests. Please wait a moment.",
    500: "Something went wrong on our side.",
    502: "The service is temporarily unreachable.",
    503: "The service is temporarily unavailable.",
    504: "The request took too long. Please try again.",
}

GENERIC_MESSAGE = "The request could not be processed."

# `error` 안에서 **최상위로 유지**하는 키 (E1-b 화이트리스트).
# 이미 3-repo 에 배포된 계약이 이 이름을 최상위에서 읽는다 —
# `params` 아래로 내리면 스케줄 겹침 확인(retry.force)·검증 목록이 조용히 죽는다.
_WHITELIST_KEYS = ("errors", "warnings", "retry", "hint")
# 봉투가 자체 필드로 흡수하므로 `params` 로 내리지 않는 키.
_CONSUMED_KEYS = ("code", "message")


def generic_code_for(status_code: int, exc: BaseException | None = None) -> str:
    """예외 클래스 → status 순으로 일반 코드를 정한다 (문구는 보지 않는다)."""
    if exc is not None:
        for klass, code in _CLASS_CODES:
            if isinstance(exc, klass):
                return code
    return _STATUS_CODES.get(status_code, f"HTTP_{status_code}")


def _status_message(status_code: int) -> str:
    return _STATUS_MESSAGES.get(status_code, GENERIC_MESSAGE)


# ---------------------------------------------------------------------------
# 봉투 조립
# ---------------------------------------------------------------------------


def build_error(
    *,
    status_code: int,
    code: str,
    code_source: str,
    message: str,
    hint: str | None = None,
    params: dict[str, Any] | None = None,
    extras: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """`error` 객체 하나를 만든다 — 봉투의 **정본**.

    extras 는 화이트리스트(errors/warnings/retry)처럼 최상위를 유지해야 하는 키.
    """
    error: dict[str, Any] = {
        "code": code,
        "code_source": code_source,
        "message": message,
        "hint": hint,
        "params": params or {},
        "trace_id": trace_id or current_trace_id(),
    }
    if extras:
        for key, value in extras.items():
            # 봉투 고정 필드는 extras 로 덮어쓰지 않는다.
            if key not in error or key == "hint":
                error[key] = value
    # status_code 는 HTTP status 로 이미 전달되므로 body 에 중복하지 않는다.
    return error


def envelope(detail: Any, error: dict[str, Any]) -> dict[str, Any]:
    """최종 응답 본문. `detail` 은 **주어진 값 그대로** 실린다 (변형 금지)."""
    return {"detail": detail, "error": error}


def error_from_http_exception(exc: StarletteHTTPException) -> dict[str, Any]:
    """HTTPException(문자열/dict/기타 detail) → `error` 객체.

    `exc.detail` 은 읽기만 한다. 호출자가 그대로 `detail` 에 미러링한다.
    """
    status_code = exc.status_code
    detail = exc.detail
    trace_id = current_trace_id()

    if isinstance(detail, dict):
        raw_code = detail.get("code")
        if isinstance(raw_code, str) and raw_code:
            code = raw_code
            code_source = "domain"
        else:
            code = generic_code_for(status_code, exc)
            code_source = "status"

        extras = {k: detail[k] for k in _WHITELIST_KEYS if k in detail}
        params = {
            k: v
            for k, v in detail.items()
            if k not in _CONSUMED_KEYS and k not in _WHITELIST_KEYS
        }

        raw_message = detail.get("message")
        if isinstance(raw_message, str) and raw_message:
            message = raw_message
        elif code_source == "domain":
            # `{"code": ...}` 만 던지는 59곳. 여기서 `message = code` 로 두면
            # HTMA 화면(고객이 보는 키오스크)에 `"username_taken"` 같은 lower_snake
            # **원문이 그대로** 뜬다. 레지스트리에 사람이 읽을 문구가 이미 선언돼 있으므로
            # 그것을 쓴다. `params` 를 넘기는 이유는 `"{step_minutes}-minute steps"` 같은
            # 자리표시자를 채우기 위해서다.
            #
            # 레지스트리에도 없으면 **코드 원문**으로 남긴다 — 그럴듯한 일반 문구
            # ("Bad request.")를 지어내면 아무도 그 지점을 고치지 않는다(E1-d).
            # 문구를 지어내지 않는 것이 참조 구현(`schedule_codes.fallback_message`)의 원칙이다.
            message = error_codes.fallback_message(code, **params)
        else:
            message = _status_message(status_code)

        hint = extras.pop("hint", None)
        return build_error(
            status_code=status_code,
            code=code,
            code_source=code_source,
            message=message,
            hint=hint if isinstance(hint, str) else None,
            params=params,
            extras=extras,
            trace_id=trace_id,
        )

    if isinstance(detail, str) and detail:
        return build_error(
            status_code=status_code,
            code=generic_code_for(status_code, exc),
            code_source="status",
            message=detail,
            trace_id=trace_id,
        )

    # list 등 그 밖의 모양 — 원형은 detail 로 나가고, error 에는 params 로만 싣는다.
    params: dict[str, Any] = {} if detail is None else {"items": detail}
    return build_error(
        status_code=status_code,
        code=generic_code_for(status_code, exc),
        code_source="status",
        message=_status_message(status_code),
        params=params,
        trace_id=trace_id,
    )


def validation_fields(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FastAPI 422 원형 배열 → `params.fields` 정규화.

    `loc` 은 `("body", "n")` 처럼 위치 접두사를 포함한다. 클라 폼은 필드명만 필요하므로
    마지막 문자열 요소를 필드명으로 쓰고, 전체 경로도 함께 준다(중첩 폼 대비).
    """
    fields: list[dict[str, Any]] = []
    for item in errors:
        loc = item.get("loc") or []
        parts = [str(p) for p in loc]
        name = next((p for p in reversed(parts) if not p.isdigit()), "")
        fields.append(
            {
                "field": name,
                "loc": parts,
                "reason": item.get("type", "invalid"),
                "message": item.get("msg", ""),
            }
        )
    return fields


# ---------------------------------------------------------------------------
# 전역 예외 핸들러 (G1) — 개인 규율에 의존하지 않는 유일한 층
# ---------------------------------------------------------------------------
# 798곳의 `raise` 를 일괄 교체하지 않는다(X8). 하다 말면 구·신 형태가 섞여 지금보다
# 나쁘다. **출구 한 곳만** 고치면 첫날부터 모든 응답이 같은 모양이 된다.

import logging  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.encoders import jsonable_encoder  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, Response  # noqa: E402
from pydantic import ValidationError as PydanticValidationError  # noqa: E402

from app.config import settings  # noqa: E402

# uvicorn.error 로거를 쓰는 이유 — uvicorn 은 자기 로거에만 핸들러를 붙인다.
# 새 이름의 로거를 만들면 root 에 핸들러가 없어 lastResort 로 떨어지고, 배포 환경의
# 로그 포맷/수집에서 벗어난다. 이 저장소의 다른 부팅 로그도 같은 로거를 쓴다.
logger = logging.getLogger("uvicorn.error")

# HTML 평면(브라우저가 직접 보는 페이지) — 여기에 JSON 을 뱉으면 화면이 깨진다(X5).
# backoffice 는 HTTPException 0건 / HTMLResponse 54건이고, /setup 도 HTML 페이지다.
_HTML_PATHS = ("/setup", "/docs", "/redoc")


# 예외 타입·메시지를 응답에 실어도 되는 환경.
#
# staging 을 뺀 이유 — staging 은 인터넷에 열려 있어서 내부 예외 메시지(테이블명,
# 파일 경로, 값)가 아무에게나 나간다. 그리고 배포된 환경에서는 debug 필드가 필요 없다:
# trace_id 로 로그를 짚으면 traceback 전체를 볼 수 있다. 그게 trace_id 를 만든 이유다.
# 로컬은 다르다 — 로그를 안 보는 경우가 많아 화면이 유일한 단서다(E7-다).
_DEBUG_ENVS = frozenset({"local", "development", "dev", "test"})


def _is_debug_env() -> bool:
    return (settings.APP_ENV or "").strip().lower() in _DEBUG_ENVS


def _wants_html(request: Request) -> bool:
    """이 요청이 HTML 평면인가.

    판정 순서가 중요하다 — `/api/` 아래는 브라우저가 Accept: text/html 을 보내는
    경우(폼 네비게이션 등)에도 항상 JSON 이어야 한다. 반대로 backoffice 비밀경로는
    경로만으로 판정 가능하다.
    """
    path = request.url.path
    if path.startswith("/api/"):
        return False
    if settings.backoffice_enabled:
        prefix = "/" + settings.BACKOFFICE_PATH.strip("/")
        if path == prefix or path.startswith(prefix + "/"):
            return True
    if path.startswith(_HTML_PATHS):
        return True
    return "text/html" in (request.headers.get("accept") or "")


def _html_error_response(
    status_code: int, error: dict[str, Any], headers: dict[str, str] | None
) -> HTMLResponse:
    """HTML 평면용 최소 오류 페이지 — 코드와 trace_id 를 화면에 남긴다."""
    import html as _html

    body = (
        "<!doctype html><meta charset='utf-8'>"
        "<title>Error</title>"
        "<div style=\"font:14px/1.6 system-ui;max-width:34rem;margin:15vh auto;"
        'padding:0 1rem;color:#222">'
        f"<h1 style=\"font-size:1.1rem\">{status_code}</h1>"
        f"<p>{_html.escape(str(error['message']))}</p>"
        f"<p style=\"color:#888;font-size:12px\">{_html.escape(error['code'])} &middot; "
        f"{_html.escape(error['trace_id'])}</p></div>"
    )
    return HTMLResponse(content=body, status_code=status_code, headers=headers)


def _respond(
    request: Request,
    status_code: int,
    detail: Any,
    error: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> Response:
    """봉투 응답 하나를 만든다. `exc.headers` 는 반드시 그대로 전달한다(X6).

    401 재인증(WWW-Authenticate) 11곳이 헤더에 의존한다 — 빠지면 조용히 죽는다.
    """
    out_headers = dict(headers or {})
    out_headers.setdefault(REQUEST_ID_HEADER, error["trace_id"])

    if _wants_html(request):
        return _html_error_response(status_code, error, out_headers)

    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(envelope(detail, error)),
        headers=out_headers,
    )


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    """HTTPException(문자열 666 / dict 132) → 봉투. `detail` 은 원형 그대로 미러링."""
    assert isinstance(exc, StarletteHTTPException)
    # 204/304 는 본문을 가질 수 없다 — Starlette 기본 동작을 유지한다.
    if exc.status_code in (204, 304):
        return Response(status_code=exc.status_code, headers=exc.headers)

    error = error_from_http_exception(exc)
    if exc.status_code >= 500:
        logger.error(
            "[error][%s] %s %s -> %s %s",
            error["trace_id"],
            request.method,
            request.url.path,
            exc.status_code,
            error["code"],
        )
    return _respond(request, exc.status_code, exc.detail, error, exc.headers)


async def validation_exception_handler(request: Request, exc: Exception) -> Response:
    """422 — `detail` 은 FastAPI 원형 배열 유지, `error.params.fields` 로 정규화."""
    assert isinstance(exc, RequestValidationError)
    raw = jsonable_encoder(exc.errors())
    error = build_error(
        status_code=422,
        code="VALIDATION_ERROR",
        code_source="status",
        message=_status_message(422),
        params={"fields": validation_fields(raw)},
    )
    return _respond(request, 422, raw, error)


async def value_error_handler(request: Request, exc: Exception) -> Response:
    """ValueError → 422 승격.

    수동 `UUID(x)` 파싱이 69곳인데 try 가 없어 지금은 전부 500 으로 떨어진다
    (`app/api/console/schedules.py` `_csv_uuids` 등). 사용자가 잘못 넣은 값이
    서버 장애처럼 보이는 것을 막는다.

    ⚠️ pydantic `ValidationError` 는 `ValueError` 의 하위 클래스다. 응답 모델
    직렬화 실패 같은 **서버 버그**가 422 로 둔갑하면 원인을 영영 못 찾으므로
    여기서 걸러 500 경로로 넘긴다.
    """
    if isinstance(exc, PydanticValidationError):
        return await unhandled_exception_handler(request, exc)

    error = build_error(
        status_code=422,
        code="INVALID_PARAMETER",
        code_source="status",
        message="Some values in the request are not valid.",
        hint="Please check the values you entered and try again.",
        params={"debug": f"{type(exc).__name__}: {exc}"} if _is_debug_env() else {},
    )
    # traceback 을 남기는 이유 — 이 경로로 오는 ValueError 는 대부분 **가드 없는
    # 파싱 지점**(69곳)이라 위치를 알아야 고칠 수 있다. 또한 `schedule_codes.issue()`
    # 처럼 미등록 코드에 ValueError 를 던지는 개발자 실수 가드가 조용한 422 로
    # 묻히지 않게 한다.
    logger.warning(
        "[error][%s] %s %s -> 422 INVALID_PARAMETER: %s",
        error["trace_id"],
        request.method,
        request.url.path,
        exc,
        exc_info=True,
    )
    return _respond(request, 422, error["message"], error)


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """미포착 예외 → 500 봉투.

    지금은 `text/plain "Internal Server Error"` 가 나가서 JSON 파싱 자체가 실패한다.
    `detail` 에 사람이 읽을 문자열을 채우는 것만으로 콘솔/앱 파서가 되살아난다.

    **`logger.exception()` 은 이 트랙의 존재 이유다** — Axiom 은 traceback 을 남기지
    않고(타입+메시지 300자), `settings.DEBUG` 면 아예 비활성이다. 화면의 trace_id 로
    로그를 짚으려면 여기서 같은 id 와 함께 traceback 을 남겨야 한다.
    """
    error = build_error(
        status_code=500,
        code="INTERNAL_ERROR",
        code_source="status",
        message=_status_message(500),
        hint="Please try again. If it keeps happening, share this reference.",
        params={"debug": f"{type(exc).__name__}: {exc}"} if _is_debug_env() else {},
    )
    logger.exception(
        "[error][%s] Unhandled error on %s %s",
        error["trace_id"],
        request.method,
        request.url.path,
    )
    return _respond(request, 500, error["message"], error)


def register_error_handlers(app: FastAPI) -> None:
    """전역 예외 핸들러 등록 — 이 앱의 **유일한 에러 출구**."""
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(ValueError, value_error_handler)
    # Exception 핸들러는 Starlette ServerErrorMiddleware(최외곽)가 쓴다.
    # 응답을 보낸 뒤 예외를 재발생시키므로 uvicorn/Axiom 쪽 기록도 그대로 남는다.
    app.add_exception_handler(Exception, unhandled_exception_handler)
