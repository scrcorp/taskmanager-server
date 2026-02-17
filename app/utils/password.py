"""비밀번호 해싱 및 검증 유틸리티 모듈.

Password hashing and verification utility module.
Uses bcrypt directly for secure password storage.
Passwords are never stored in plain text — always hashed with bcrypt.
"""

import bcrypt


def hash_password(password: str) -> str:
    """평문 비밀번호를 bcrypt 해시로 변환합니다.

    Hash a plain text password using bcrypt.
    The resulting hash includes a random salt, making each hash unique
    even for identical passwords.

    Args:
        password: 평문 비밀번호 (Plain text password to hash)

    Returns:
        str: bcrypt 해시 문자열 (Bcrypt hash string, ~60 chars)

    Example:
        hashed = hash_password("my-secret-password")
        # "$2b$12$LJ3m4ys3..."
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """평문 비밀번호와 bcrypt 해시를 비교 검증합니다.

    Verify a plain text password against a bcrypt hash.
    Uses constant-time comparison to prevent timing attacks.

    Args:
        plain_password: 검증할 평문 비밀번호 (Plain text password to verify)
        hashed_password: 저장된 bcrypt 해시 (Stored bcrypt hash to compare against)

    Returns:
        bool: 일치하면 True, 불일치하면 False (True if password matches hash)
    """
    return bcrypt.checkpw(
        plain_password.encode("utf-8"), hashed_password.encode("utf-8")
    )
