"""Backoffice 세션 토큰 — HMAC 서명 쿠키 (외부 의존성 없음).

Stateless signed session for the backoffice. No DB, no itsdangerous —
just HMAC-SHA256 over `username|expiry`, base64url-encoded.

Token format:  base64url(payload) + "." + base64url(hmac_sig)
  payload = "<username>|<exp_unix_ts>"
"""

import base64
import hashlib
import hmac
import time

from app.config import settings


def _b64e(raw: bytes) -> str:
    """패딩 없는 base64url 인코딩."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    """패딩 복원 후 base64url 디코딩."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes) -> bytes:
    secret = settings.backoffice_session_secret.encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).digest()


def issue_session(username: str) -> str:
    """운영자 username에 대해 서명된 세션 토큰 발급."""
    exp = int(time.time()) + settings.BACKOFFICE_SESSION_MAX_AGE_MINUTES * 60
    payload = f"{username}|{exp}".encode("utf-8")
    return f"{_b64e(payload)}.{_b64e(_sign(payload))}"


def verify_session(token: str) -> str | None:
    """토큰 검증 → 유효하면 username, 아니면 None.

    검사: 서명(constant-time) → 만료 → username이 현재 설정된 운영자와 일치
    (자격증명 교체 시 기존 세션 무효화).
    """
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _b64d(payload_b64)
        if not hmac.compare_digest(_b64d(sig_b64), _sign(payload)):
            return None
        username, exp_str = payload.decode("utf-8").rsplit("|", 1)
        if int(exp_str) < int(time.time()):
            return None
        if username != settings.BACKOFFICE_ADMIN_USERNAME:
            return None
        return username
    except (ValueError, TypeError):
        return None
