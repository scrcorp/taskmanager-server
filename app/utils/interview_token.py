"""인터뷰 스케줄링 공개 링크 토큰.

지원자는 로그인 없이 이메일 토큰 링크로 희망 시간을 고른다. 서명 JWT(stateless) +
application 에 저장한 jti(nonce) 매칭으로 무효화/회전 지원.

토큰 클레임: {sub: application_id, purpose: "interview_schedule", jti, exp}
- verify 는 서명/만료/purpose 를 검사하고 (application_id, jti) 를 돌려준다.
- 호출 측에서 application.interview_token == jti 인지 한 번 더 확인 (회전/취소 시 불일치 → 거부).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt

from app.config import settings

PURPOSE = "interview_schedule"
EXPIRE_DAYS = 14


def issue_interview_token(application_id: UUID) -> tuple[str, str]:
    """새 인터뷰 토큰 발급. (token, jti) 반환 — jti 를 application.interview_token 에 저장한다."""
    jti = uuid4().hex
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(application_id),
        "purpose": PURPOSE,
        "jti": jti,
        "iat": now,
        "exp": now + timedelta(days=EXPIRE_DAYS),
    }
    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, jti


def decode_interview_token(token: str) -> tuple[UUID, str]:
    """토큰 검증 → (application_id, jti). 서명/만료/purpose 불일치 시 예외.

    Raises:
        jwt.ExpiredSignatureError: 만료
        jwt.InvalidTokenError: 서명 오류 / purpose 불일치 / 형식 오류
    """
    payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("purpose") != PURPOSE:
        raise jwt.InvalidTokenError("wrong token purpose")
    jti = payload.get("jti")
    sub = payload.get("sub")
    if not jti or not sub:
        raise jwt.InvalidTokenError("missing claims")
    return UUID(sub), jti
