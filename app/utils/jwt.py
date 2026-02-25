"""JWT 토큰 생성 및 검증 유틸리티 모듈.

JWT token creation and verification utility module.
Provides functions for creating access/refresh tokens and decoding them.

JWT Payload Structure:
    액세스/리프레시 토큰 모두 동일한 기본 페이로드를 사용합니다.
    Both access and refresh tokens share the same base payload:
    {
        "sub": "user_uuid",         # 사용자 ID (User identifier)
        "org": "organization_uuid", # 조직 ID (Organization identifier)
        "role": "owner",            # 역할 이름 (Role name)
        "level": 1,                 # 역할 레벨 (Role permission level)
        "exp": 1234567890,          # 만료 시간 UNIX timestamp (Expiration)
        "type": "access"|"refresh"  # 토큰 유형 (Token type discriminator)
    }
"""

from datetime import datetime, timedelta, timezone
from typing import Any
import jwt

from app.config import settings


def create_access_token(data: dict[str, Any]) -> str:
    """JWT 액세스 토큰을 생성합니다.

    Generate a JWT access token with the given payload data.
    Token expires after JWT_ACCESS_TOKEN_EXPIRE_MINUTES (default: 30 min).

    Args:
        data: JWT 페이로드 데이터. 일반적으로 {"sub": user_id, "org": org_id, "role": role_name, "priority": role_priority}
              (JWT payload data, typically contains user/org/role info)

    Returns:
        str: 인코딩된 JWT 문자열 (Encoded JWT token string)

    Example:
        token = create_access_token({"sub": str(user.id), "org": str(user.organization_id)})
    """
    to_encode: dict[str, Any] = data.copy()
    # 만료 시간 설정 — 현재 UTC 시간 + 설정된 분 수 (Set expiration from current UTC + configured minutes)
    expire: datetime = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(data: dict[str, Any]) -> str:
    """JWT 리프레시 토큰을 생성합니다.

    Generate a JWT refresh token with the given payload data.
    Token expires after JWT_REFRESH_TOKEN_EXPIRE_DAYS (default: 7 days).
    Refresh tokens are used to obtain new access tokens without re-login.

    Args:
        data: JWT 페이로드 데이터 (JWT payload data, same structure as access token)

    Returns:
        str: 인코딩된 JWT 리프레시 토큰 문자열 (Encoded JWT refresh token string)
    """
    to_encode: dict[str, Any] = data.copy()
    # 만료 시간 설정 — 현재 UTC 시간 + 설정된 일 수 (Set expiration from current UTC + configured days)
    expire: datetime = datetime.now(timezone.utc) + timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """JWT 토큰을 디코딩하고 검증합니다.

    Decode and verify a JWT token string.
    Raises jwt.ExpiredSignatureError if the token has expired,
    and jwt.InvalidTokenError for any other validation failure.

    Args:
        token: JWT 토큰 문자열 (Encoded JWT token string)

    Returns:
        dict[str, Any]: 디코딩된 페이로드 딕셔너리 (Decoded payload dictionary)

    Raises:
        jwt.ExpiredSignatureError: 토큰 만료 시 (When token has expired)
        jwt.InvalidTokenError: 유효하지 않은 토큰 (When token is invalid)
    """
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
