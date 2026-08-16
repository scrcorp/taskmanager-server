"""요청이 들어온 클라이언트 표면(채널) — 감사 기록용 contextvar.

"누가(actor)"는 이미 기록되지만 "어느 경로로(console 웹 / /c 컴팩트 / HTMA / cron)"는
어디에도 남지 않았다 (2026-08-14 recon). 같은 수정이라도 경로가 다르면 운영 맥락이
다르므로, 감사 행(attendance_corrections, empid_changes)에 채널을 함께 남긴다.

동작:
- ClientSurfaceMiddleware 가 요청마다 contextvar 를 설정한다.
  기본값은 경로 prefix 로 판정하고, 신뢰 가능한 세분화(console vs /c)는
  `X-Client-Surface` 요청 헤더(허용 목록 내 값만)로 덮어쓴다.
- 요청 밖(cron 등)에서는 contextvar 가 비어 있고, 소비자는 "system" 으로 기록한다.

헤더는 클라이언트 자기신고라 보안 경계가 아니다 — 감사 편의 메타데이터일 뿐이며,
허용 목록 밖 값은 무시한다 (경로 기본값 유지).
"""

from __future__ import annotations

from contextvars import ContextVar

# 채널 값 — 감사 행에 그대로 저장된다 (String(20) 안에 들어가는 짧은 코드).
CHANNEL_CONSOLE = "console"            # 콘솔 웹
CHANNEL_CONSOLE_COMPACT = "console_compact"  # /c 간소화 콘솔 (콘솔 API 재사용이라 헤더로만 구분)
CHANNEL_HTMA = "htma"                  # HTMA 근태 기기 (디바이스 + manage)
CHANNEL_STAFF_APP = "staff_app"        # Staff 앱
CHANNEL_BACKOFFICE = "backoffice"      # 백오피스 도구
CHANNEL_SYSTEM = "system"              # cron / 서버 내부 (요청 컨텍스트 없음)
CHANNEL_API = "api"                    # 그 외 직접 호출

# 헤더로 덮어쓸 수 있는 값 — 경로만으로 구분 불가능한 세분화만 허용한다.
# (예: /c 는 콘솔 API 를 그대로 쓰므로 헤더 없이는 console 로 보인다.)
_HEADER_ALLOWED = {CHANNEL_CONSOLE, CHANNEL_CONSOLE_COMPACT}

_current_channel: ContextVar[str | None] = ContextVar("client_surface", default=None)


def channel_from_path(path: str) -> str:
    """요청 경로 → 기본 채널. 미들웨어와 테스트가 공유하는 단일 판정."""
    if path.startswith("/api/v1/console"):
        return CHANNEL_CONSOLE
    if path.startswith("/api/v1/attendance"):
        return CHANNEL_HTMA
    if path.startswith("/api/v1/app"):
        return CHANNEL_STAFF_APP
    if path.startswith("/backoffice") or path.startswith("/api/backoffice"):
        return CHANNEL_BACKOFFICE
    return CHANNEL_API


def resolve_channel(path: str, header_value: str | None) -> str:
    """경로 기본값 + 허용된 헤더 오버라이드."""
    base = channel_from_path(path)
    if header_value:
        v = header_value.strip().lower()
        if v in _HEADER_ALLOWED:
            return v
    return base


def set_current_channel(value: str | None) -> None:
    _current_channel.set(value)


def current_channel(default: str = CHANNEL_SYSTEM) -> str:
    """지금 실행 맥락의 채널. 요청 밖(cron 등)이면 default("system")."""
    return _current_channel.get() or default
