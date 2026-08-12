"""Axiom API 로깅 미들웨어.

Axiom API logging middleware.
Captures request/response data and sends structured logs to Axiom.
Logs: API endpoint, method, data (body/params), status code, error reason.
Sensitive fields (password, token, secret) are automatically masked.
"""

import hashlib
import time
import json
import re
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from axiom_py import Client as AxiomClient

from app.config import settings
from app.core.error_envelope import current_trace_id

# 마스킹 대상 필드 패턴 — Fields to mask in request/response bodies
_SENSITIVE_KEYS = re.compile(
    r"(password|passwd|secret|token|authorization|api_key|apikey|access_token|refresh_token|credential)",
    re.IGNORECASE,
)

# 로깅 제외 경로 — Paths excluded from logging
_SKIP_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}

# 기기 식별 키를 남길 경로 — attendance 기기(HTMA)만 대상.
_DEVICE_KEY_PREFIX = "/api/v1/attendance"


def _device_key(request: Request) -> str | None:
    """기기 토큰의 sha256 앞 12자.

    왜 필요한가 — `detail` 을 제거하려면 "구버전 호출 0건이 N일 지속"을 판정해야 하고,
    그러려면 **기기별** 마지막 앱 버전을 알아야 한다. 서버는 `attendance_devices.token_hash`
    로 sha256 전체를 이미 보관하므로, 로그에 그 앞 12자만 남기면 새 테이블 없이
    기기 row 와 join 된다.

    평문 토큰이 아니라 해시 접두사라 되돌릴 수 없다 — 로그가 자격증명이 되지 않는다.
    """
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def _mask_dict(data: Any, depth: int = 0) -> Any:
    """민감 필드 자동 마스킹 — Recursively mask sensitive fields in dicts/lists."""
    if depth > 5:
        return "..."
    if isinstance(data, dict):
        return {
            k: "***" if _SENSITIVE_KEYS.search(k) else _mask_dict(v, depth + 1)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_mask_dict(item, depth + 1) for item in data[:20]]
    return data


def _truncate(value: Any, max_len: int = 2000) -> Any:
    """로그 크기 제한 — Truncate large values to prevent oversized logs."""
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + "...(truncated)"
    return value


class AxiomLoggingMiddleware(BaseHTTPMiddleware):
    """모든 API 요청/응답을 Axiom에 로깅하는 미들웨어.

    Middleware that logs all API requests and responses to Axiom.
    Captures: method, path, query params, request body, status code, error detail.
    """

    def __init__(self, app: Any) -> None:
        super().__init__(app)
        self._client: AxiomClient | None = None
        self._dataset: str = settings.AXIOM_DATASET

        if not settings.DEBUG and settings.AXIOM_API_TOKEN and settings.AXIOM_DATASET:
            self._client = AxiomClient(token=settings.AXIOM_API_TOKEN)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # 제외 경로 스킵 — Skip excluded paths
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        # Axiom 미설정시 패스스루 — Pass through if Axiom not configured
        if not self._client:
            return await call_next(request)

        start_time = time.time()

        # 요청 데이터 수집 — Collect request data
        method = request.method
        path = request.url.path
        query_params = dict(request.query_params) if request.query_params else None
        path_params = dict(request.path_params) if request.path_params else None

        # Request body 읽기 — Read request body (only for methods with body)
        request_body: Any = None
        if method in ("POST", "PUT", "PATCH"):
            try:
                body_bytes = await request.body()
                if body_bytes:
                    request_body = json.loads(body_bytes)
                    request_body = _mask_dict(request_body)
                    request_body = _truncate(request_body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                request_body = "(non-json body)"

        # 응답 처리 — Process response
        error_detail: str | None = None
        error_code: str | None = None
        status_code: int = 500
        try:
            response = await call_next(request)
            status_code = response.status_code

            # 에러 응답시 body에서 사유 추출 — Extract error detail from error responses
            if status_code >= 400:
                resp_body = b""
                async for chunk in response.body_iterator:
                    if isinstance(chunk, bytes):
                        resp_body += chunk
                    else:
                        resp_body += chunk.encode("utf-8")

                try:
                    error_data = json.loads(resp_body)
                    error_detail = error_data.get("detail", str(error_data))
                    # 봉투의 정본은 `error` 다. 코드를 따로 남겨야 Axiom 에서
                    # code 별 집계(= code_source status 비율 감소 곡선)가 가능하다.
                    envelope_error = error_data.get("error")
                    if isinstance(envelope_error, dict):
                        code = envelope_error.get("code")
                        source = envelope_error.get("code_source")
                        if isinstance(code, str):
                            error_code = (
                                f"{code}:{source}" if isinstance(source, str) else code
                            )
                    if isinstance(error_detail, str) and len(error_detail) > 500:
                        error_detail = error_detail[:500] + "..."
                except (json.JSONDecodeError, UnicodeDecodeError):
                    error_detail = resp_body.decode("utf-8", errors="replace")[:500]

                # 소비한 body를 다시 응답으로 반환 — Re-wrap consumed body
                from starlette.responses import Response as StarletteResponse

                response = StarletteResponse(
                    content=resp_body,
                    status_code=status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                )
        except Exception as exc:
            error_detail = f"{type(exc).__name__}: {str(exc)[:300]}"
            raise
        finally:
            duration_ms = round((time.time() - start_time) * 1000, 2)

            # Axiom 로그 이벤트 구성 — Build Axiom log event
            log_event: dict[str, Any] = {
                "method": method,
                "path": path,
                "status_code": status_code,
                "duration_ms": duration_ms,
                # 화면에 뜬 id 로 로그를 짚는 것이 이 트랙의 핵심 가치다.
                # 봉투(error.trace_id) / 헤더(X-Request-Id) / 이 필드가 모두 같은 값.
                "trace_id": current_trace_id(),
            }

            # 앱 버전 텔레메트리 — `detail` 레거시 미러를 언제 지울 수 있는지 판단하는
            # 유일한 근거. "구버전 호출 0건이 N일 지속"을 이 두 필드로 집계한다.
            # (X-App-Version 을 보내지 않는 구버전은 필드 자체가 없다 = 그것이 신호다)
            app_version = request.headers.get("X-App-Version")
            if app_version:
                log_event["app_version"] = app_version
            if path.startswith(_DEVICE_KEY_PREFIX):
                device_key = _device_key(request)
                if device_key:
                    log_event["device_key"] = device_key

            if query_params:
                log_event["query_params"] = _mask_dict(query_params)
            if path_params:
                log_event["path_params"] = path_params
            if request_body is not None:
                log_event["request_body"] = request_body
            if error_detail:
                log_event["error"] = error_detail
            if error_code:
                log_event["error_code"] = error_code

            # Axiom 전송 (비동기 ingest) — Send to Axiom
            try:
                self._client.ingest_events(self._dataset, [log_event])
            except Exception:
                pass  # 로깅 실패가 요청 처리에 영향주지 않도록 — Never break request on log failure

        return response
