"""Backoffice 인증 의존성 — org 권한 시스템과 완전 독립.

Auth helpers for the backoffice. Reads the signed session cookie only —
no JWT, no org/role lookup. This is the separate operator plane.
"""

from fastapi import Request

from app.api.backoffice.session import verify_session

# 세션 쿠키 이름 — devtools 가독성 위해 대문자 접두사 (feedback_uppercase_keys 정신)
COOKIE_NAME: str = "HTM_BO_SESSION"


def get_current_admin(request: Request) -> str | None:
    """세션 쿠키에서 운영자 username 추출 → 미인증이면 None.

    의존성으로 raise 하지 않고 None을 반환한다 — HTML 페이지에서
    None이면 라우트가 로그인으로 redirect 하는 UX를 위해.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return verify_session(token)
