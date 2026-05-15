"""Attendance 응답에 앱 버전 정보를 piggyback 헤더로 실어 보낸다.

키오스크는 어차피 출퇴근/스케줄/today-staff 등을 수시로 호출하므로,
별도 폴링 없이 매 응답마다 X-App-* 헤더를 추가해 near real-time 업데이트
알림이 가능. DB 부담은 5분 메모리 캐시로 0에 수렴.

헤더:
    X-App-Latest-Version: 최신 릴리스 semver (없으면 미발신)
    X-App-Min-Version:    강제 차단 floor semver (없으면 미발신)
    X-App-Download-Url:   latest APK pre-signed URL (없으면 미발신)
"""

import time
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.database import async_session
from app.services.app_version_service import app_version_service


_PREFIX = "/api/v1/attendance"
_CACHE_TTL_SECONDS = 300  # 5분


class AppVersionBroadcastMiddleware(BaseHTTPMiddleware):
    """Attendance API 응답에 X-App-* 헤더 추가."""

    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._cache: Optional[dict[str, Optional[str]]] = None
        self._cache_ts: float = 0.0

    async def _get_cached(self) -> dict[str, Optional[str]]:
        """채널 latest/min/download_url 5분 캐시."""
        now = time.time()
        if self._cache is not None and (now - self._cache_ts) < _CACHE_TTL_SECONDS:
            return self._cache
        async with async_session() as db:
            channel = app_version_service.attendance_channel()
            latest, min_version = await app_version_service.get_for_channel(db, channel)
            result: dict[str, Optional[str]] = {
                "latest": latest.version if latest else None,
                "min": min_version,
                "url": (
                    app_version_service.presigned_download_url(latest.s3_key)
                    if latest
                    else None
                ),
            }
        self._cache = result
        self._cache_ts = now
        return result

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        if not request.url.path.startswith(_PREFIX):
            return response
        try:
            cached = await self._get_cached()
            if cached["latest"]:
                response.headers["X-App-Latest-Version"] = cached["latest"]
            if cached["min"]:
                response.headers["X-App-Min-Version"] = cached["min"]
            if cached["url"]:
                response.headers["X-App-Download-Url"] = cached["url"]
        except Exception:
            # 버전 broadcast 실패는 실제 응답을 막지 않는다.
            pass
        return response
