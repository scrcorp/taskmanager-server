"""Unit tests for app.utils.interview_token — 인터뷰 공개 링크 토큰.

DB 무관 (서명 JWT). 분기 전수:
  - roundtrip (issue → decode)
  - jti 회전 (발급마다 새 jti)
  - purpose 불일치 거부
  - 필수 claim 누락 거부
  - 변조/잘못된 서명 거부
  - 만료 거부
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest

from app.config import settings
from app.utils.interview_token import (
    EXPIRE_DAYS,
    PURPOSE,
    decode_interview_token,
    issue_interview_token,
)


def _encode(payload: dict, key: str | None = None) -> str:
    return jwt.encode(
        payload,
        key or settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


class TestRoundtrip:
    def test_issue_decode_roundtrip(self):
        app_id = uuid4()
        token, jti = issue_interview_token(app_id)
        decoded_id, decoded_jti = decode_interview_token(token)
        assert decoded_id == app_id
        assert decoded_jti == jti

    def test_jti_rotates_each_issue(self):
        app_id = uuid4()
        _, jti1 = issue_interview_token(app_id)
        _, jti2 = issue_interview_token(app_id)
        assert jti1 != jti2  # 회전마다 새 jti → 기존 링크 무효화 가능


class TestRejects:
    def test_wrong_purpose_rejected(self):
        now = datetime.now(timezone.utc)
        bad = _encode({
            "sub": str(uuid4()), "purpose": "password_reset",
            "jti": uuid4().hex, "iat": now, "exp": now + timedelta(days=1),
        })
        with pytest.raises(jwt.InvalidTokenError):
            decode_interview_token(bad)

    def test_missing_claims_rejected(self):
        now = datetime.now(timezone.utc)
        bad = _encode({"purpose": PURPOSE, "iat": now, "exp": now + timedelta(days=1)})
        with pytest.raises(jwt.InvalidTokenError):
            decode_interview_token(bad)

    def test_tampered_signature_rejected(self):
        now = datetime.now(timezone.utc)
        wrong = _encode(
            {
                "sub": str(uuid4()), "purpose": PURPOSE,
                "jti": uuid4().hex, "iat": now, "exp": now + timedelta(days=1),
            },
            key="a-different-secret-key-entirely",
        )
        with pytest.raises(jwt.InvalidTokenError):
            decode_interview_token(wrong)

    def test_expired_rejected(self):
        past = datetime.now(timezone.utc) - timedelta(days=EXPIRE_DAYS + 1)
        expired = _encode({
            "sub": str(uuid4()), "purpose": PURPOSE,
            "jti": uuid4().hex, "iat": past, "exp": past + timedelta(minutes=1),
        })
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_interview_token(expired)
