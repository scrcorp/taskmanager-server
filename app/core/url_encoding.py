"""UUID ↔ base64url 런타임 인코딩.

Public signup link (예: hermesops.site/join/{encoded}) 생성/해석에 사용.
DB 변경 없이 런타임에서만 변환 — store.id (UUID, 36자) ↔ encoded (22자).

Roundtrip:
    >>> u = UUID('0e8400e2-29b1-41d4-a716-446655440000')
    >>> encoded = encode_uuid(u)  # 'B4QA4imRQdSnFkRmVUQAAA' (22자, padding 제거)
    >>> decode_uuid(encoded) == u
    True

URL-safe alphabet (RFC 4648 §5): A-Z, a-z, 0-9, '-', '_'
"""

from __future__ import annotations

import base64
import uuid


def encode_uuid(u: uuid.UUID) -> str:
    """UUID → base64url 인코딩 (22자, padding 제거).

    Args:
        u: UUID 인스턴스 (16 bytes).

    Returns:
        base64url 인코딩된 22자 문자열.
    """
    return base64.urlsafe_b64encode(u.bytes).rstrip(b"=").decode("ascii")


def decode_uuid(s: str) -> uuid.UUID:
    """base64url → UUID 디코딩.

    Args:
        s: base64url 인코딩된 문자열 (padding 유무 무관).

    Returns:
        UUID 인스턴스.

    Raises:
        ValueError: 디코딩 실패 (포맷 불일치, 길이 불일치 등).
    """
    if not isinstance(s, str) or not s:
        raise ValueError("encoded string must be a non-empty str")

    # padding 자동 보정 (base64는 4의 배수 필요)
    pad = "=" * (-len(s) % 4)
    try:
        raw = base64.urlsafe_b64decode(s + pad)
    except (ValueError, TypeError) as e:
        raise ValueError(f"invalid base64url: {e}") from e

    if len(raw) != 16:
        raise ValueError(f"decoded payload must be 16 bytes, got {len(raw)}")

    return uuid.UUID(bytes=raw)
