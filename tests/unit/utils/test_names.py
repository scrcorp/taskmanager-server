"""Unit — app/utils/names 이름 조합 단일 헬퍼.

대상:
    - compose_full_name(first, middle, last)
    - display_name(user)

[작성됨]
- compose: first+last → "First Last"
- compose: first+middle+last → "First Middle Last"
- compose: strip + 빈/None 파트 생략
- display: 구조화 완결(first+last) → 조합값 (full_name 무시)
- display: middle 포함 조합
- display: 구조화 없음 → full_name 폴백
- display: middle 만 있는 불완전 구조화 → full_name 폴백
- display: first 만 / last 만 → full_name 폴백
- display: 전부 없음 → 빈 문자열
- display: dataclass 등 임의 객체 duck-typing 허용
"""
from __future__ import annotations

from dataclasses import dataclass

from app.utils.names import compose_full_name, display_name


@dataclass
class _FakeUser:
    """User ORM 대역 — 이름 파트만 가진 duck-type 객체."""

    full_name: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None


# === compose_full_name ===

def test_compose_first_last() -> None:
    assert compose_full_name("John", None, "Doe") == "John Doe"


def test_compose_first_middle_last() -> None:
    assert compose_full_name("John", "Quincy", "Doe") == "John Quincy Doe"


def test_compose_strips_and_skips_empty_parts() -> None:
    assert compose_full_name("  John ", "  ", " Doe ") == "John Doe"
    assert compose_full_name("John", "", "Doe") == "John Doe"
    assert compose_full_name(None, None, None) == ""


# === display_name ===

def test_display_prefers_structured_when_first_and_last_present() -> None:
    user = _FakeUser(full_name="Stale Old Name", first_name="John", last_name="Doe")
    assert display_name(user) == "John Doe"


def test_display_includes_middle_when_structured_complete() -> None:
    user = _FakeUser(
        full_name="Whatever", first_name="John", middle_name="Q", last_name="Doe"
    )
    assert display_name(user) == "John Q Doe"


def test_display_falls_back_to_full_name_when_no_structured() -> None:
    user = _FakeUser(full_name="Jane Roe")
    assert display_name(user) == "Jane Roe"


def test_display_middle_only_falls_back_to_full_name() -> None:
    # 불완전 구조화(first/last 없음) — 조합 신뢰 불가 → full_name 폴백
    user = _FakeUser(full_name="Jane Roe", middle_name="Q")
    assert display_name(user) == "Jane Roe"


def test_display_first_only_or_last_only_falls_back() -> None:
    assert display_name(_FakeUser(full_name="Jane Roe", first_name="Jane")) == "Jane Roe"
    assert display_name(_FakeUser(full_name="Jane Roe", last_name="Roe")) == "Jane Roe"


def test_display_whitespace_structured_treated_as_absent() -> None:
    user = _FakeUser(full_name="Jane Roe", first_name="  ", last_name="  ")
    assert display_name(user) == "Jane Roe"


def test_display_all_absent_returns_empty_string() -> None:
    assert display_name(_FakeUser()) == ""
    assert display_name(_FakeUser(full_name="  ")) == ""
