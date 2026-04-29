"""UUID ↔ base64url 런타임 인코딩 테스트."""

from __future__ import annotations

import uuid

import pytest

from app.core.url_encoding import decode_uuid, encode_uuid


def test_encode_known_uuid_produces_22_chars():
    u = uuid.UUID("0e8400e2-29b1-41d4-a716-446655440000")
    encoded = encode_uuid(u)
    assert len(encoded) == 22
    assert "=" not in encoded


def test_encode_uses_urlsafe_alphabet_only():
    """A-Z, a-z, 0-9, '-', '_'만 포함 (RFC 4648 §5)."""
    for _ in range(50):
        encoded = encode_uuid(uuid.uuid4())
        for ch in encoded:
            assert ch.isalnum() or ch in "-_", f"unexpected char {ch!r} in {encoded}"


def test_roundtrip_random_uuids():
    """random UUID 100개 — encode → decode 결과 동일."""
    for _ in range(100):
        original = uuid.uuid4()
        encoded = encode_uuid(original)
        decoded = decode_uuid(encoded)
        assert decoded == original


def test_roundtrip_edge_cases():
    """uuid 경계값 — 0, max, RFC 4122 nil."""
    for original in (
        uuid.UUID(int=0),
        uuid.UUID(int=(1 << 128) - 1),
        uuid.UUID("00000000-0000-0000-0000-000000000000"),
    ):
        assert decode_uuid(encode_uuid(original)) == original


def test_decode_accepts_padding():
    """padding 있어도 동일하게 동작 (RFC는 padding 허용)."""
    u = uuid.uuid4()
    encoded = encode_uuid(u)
    with_padding = encoded + "=" * (-len(encoded) % 4)
    assert decode_uuid(with_padding) == u


def test_decode_rejects_empty():
    with pytest.raises(ValueError, match="non-empty"):
        decode_uuid("")


def test_decode_rejects_non_string():
    with pytest.raises(ValueError, match="non-empty"):
        decode_uuid(None)  # type: ignore[arg-type]


def test_decode_rejects_invalid_base64_chars():
    """완전히 base64 alphabet 밖의 문자만 — strict 디코드 실패."""
    # urlsafe_b64decode는 lenient하지만 일부 케이스는 raise.
    # 가장 안전: 완전히 빈 base64 결과를 만드는 한 글자 입력
    with pytest.raises(ValueError):
        decode_uuid("@")


def test_decode_rejects_wrong_length():
    """디코드는 됐지만 16바이트 아니면 거부."""
    # 8바이트 → 'AAAAAAAAAAA' (11자)
    half = "AAAAAAAAAAA"
    with pytest.raises(ValueError, match="16 bytes"):
        decode_uuid(half)
    # padding 있는 24바이트 → 거부
    too_long = "A" * 32
    with pytest.raises(ValueError, match="16 bytes"):
        decode_uuid(too_long)


def test_encoded_length_invariant_across_uuid_values():
    """모든 16바이트 UUID는 padding 제거 후 정확히 22자."""
    for _ in range(50):
        encoded = encode_uuid(uuid.uuid4())
        assert len(encoded) == 22
