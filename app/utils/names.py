"""이름 조합 단일 헬퍼 — first/middle/last ↔ full_name 전환기 규칙.

[Model B 이행] users 는 canonical full_name 과 구조화 이름(first/middle/last)이
병존한다. 이 모듈이 **유일한 조합 규칙**이다 — 다른 곳에서 " ".join 재구현 금지.

규칙 (composition rule):
    - 구조화 경로는 first + last 필수, middle 선택.
    - full_name = "First [Middle ]Last" (공백 1칸, 빈 파트 생략, 각 파트 strip).
    - 표시 이름(display_name)은 first/last 가 **둘 다** 있으면 조합값,
      아니면 full_name 폴백 (부분 백필/레거시 유저 호환).

Payroll: payroll_entries.member_name 스냅샷은 display_name() 을 경유한다 (스키마 스펙 E5).

NOTE: 기존 full_name 을 first/last 로 쪼개는 데이터 백필은 하지 않는다
(중간이름/성 순서 모호 — Model B 백필 마이그레이션에서 best-effort 1회 수행됨).
"""
from __future__ import annotations

from typing import Protocol


class _HasNameParts(Protocol):
    """이름 파트를 가진 객체 (User ORM, 스냅샷 dict wrapper 등)."""

    full_name: str | None
    first_name: str | None
    middle_name: str | None
    last_name: str | None


def compose_full_name(
    first: str | None, middle: str | None, last: str | None
) -> str:
    """구조화 파트를 full_name 문자열로 조합.

    "First Middle Last" — 빈/None 파트는 생략, 각 파트 strip.
    파트 검증(first/last 필수)은 호출측(스키마/서비스) 책임.
    """
    parts = ((first or "").strip(), (middle or "").strip(), (last or "").strip())
    return " ".join(p for p in parts if p)


def display_name(user: _HasNameParts) -> str:
    """표시 이름 — 구조화 이름이 완결(first+last)이면 조합, 아니면 full_name.

    payroll 스냅샷(member_name) 등 "사람이 보는 이름"은 반드시 이 함수를 쓴다.
    middle 만 있는 등 불완전 구조화 상태는 full_name 폴백 (조합 신뢰 불가).
    """
    first = (getattr(user, "first_name", None) or "").strip()
    last = (getattr(user, "last_name", None) or "").strip()
    if first and last:
        return compose_full_name(first, getattr(user, "middle_name", None), last)
    return (getattr(user, "full_name", None) or "").strip()
