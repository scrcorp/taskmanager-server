"""구버전 HTMA 어댑터의 버전 판정 (app/core/app_version_compat.py).

핵심 계약 — **모르면 구버전**이다. 헤더가 없거나 해석 불가면 예전 동작(force)을
유지한다. 반대로 하면 헤더를 안 보내는 현재 HTMA 전부가 409 를 받게 된다.
"""

from __future__ import annotations

import pytest

from app.core.app_version_compat import (
    HTMA_FORCE_FIELD_MIN_VERSION,
    effective_force,
    parse_app_version,
    supports_force_field,
)


class _FakeRequest:
    """Request 의 headers 만 흉내 — 어댑터는 헤더 외엔 아무것도 보지 않는다."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.0.17+38", (1, 0, 17)),   # build number 는 비교에 쓰지 않는다
        ("1.0.17", (1, 0, 17)),
        (" 2.10.3 ", (2, 10, 3)),
        ("1.0", None),              # semver 미달
        ("dev-build", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_app_version(raw: str | None, expected: tuple[int, int, int] | None) -> None:
    assert parse_app_version(raw) == expected


def test_missing_header_is_legacy() -> None:
    """현재 HTMA 는 요청 헤더를 보내지 않는다 — 그것이 기본 경로다."""
    req = _FakeRequest()
    assert supports_force_field(req) is False
    assert effective_force(req, requested=False) is True


def test_below_threshold_is_legacy() -> None:
    major, minor, patch = HTMA_FORCE_FIELD_MIN_VERSION
    older = f"{major}.{minor}.{patch - 1}+1"
    req = _FakeRequest({"X-App-Version": older})
    assert supports_force_field(req) is False
    assert effective_force(req, requested=False) is True


def test_threshold_and_above_respects_client() -> None:
    major, minor, patch = HTMA_FORCE_FIELD_MIN_VERSION
    for raw in (f"{major}.{minor}.{patch}", f"{major}.{minor}.{patch + 1}+99"):
        req = _FakeRequest({"X-App-Version": raw})
        assert supports_force_field(req) is True
        assert effective_force(req, requested=False) is False
        assert effective_force(req, requested=True) is True
