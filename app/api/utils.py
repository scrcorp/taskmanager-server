"""API 유틸리티 — Request에서 클라이언트 정보를 추출합니다.

API utilities — Extract client info from FastAPI Request objects.
"""

from fastapi import Request


def get_client_ip(request: Request) -> str | None:
    """Extract client IP from request, considering proxies."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def get_session_info(request: Request) -> dict[str, str | None]:
    """Extract session info (user_agent, ip_address) from request.

    Returns:
        dict with keys: user_agent, ip_address
    """
    ua_string = request.headers.get("user-agent")
    return {
        "user_agent": ua_string[:512] if ua_string else None,
        "ip_address": get_client_ip(request),
    }
