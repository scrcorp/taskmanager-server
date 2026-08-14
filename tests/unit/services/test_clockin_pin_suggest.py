"""Unit — PIN 충돌 목록 조회 + 안 쓰이는 PIN 추천.

`find_pin_conflicts` 는 콘솔 lookup 과 저장 게이트(`assert_no_pin_conflict`)가
공유하는 판정이다 — 충돌은 정확히 같은 값 하나뿐(2026-08-13 규칙 개정). `suggest_available_clockin_pin` 은 배정 없이 빈 번호만 찾아준다
(관리자가 짧은 PIN 을 직접 고를 때 쓰는 도구).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import async_session
from app.services.attendance_device_service import (
    find_pin_conflicts,
    suggest_available_clockin_pin,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def pins_1234_and_5678(
    seed_organization: dict, test_users: dict, restore_pins: None
) -> dict:
    """org 안의 PIN 을 '1234'(testsv), '5678'(testgm) 둘만 남도록 통제.

    나머지 시드 유저는 PIN 을 비운다 — 랜덤 시드 PIN 이 남아 있으면 추천 결과가
    우연히 그것과 겹치는지에 따라 테스트가 흔들린다.
    """
    async with async_session() as db:
        for username, pin in (
            ("testsv", "1234"),
            ("testgm", "5678"),
            ("testadmin", None),
            ("teststaff", None),
        ):
            await db.execute(
                text("UPDATE users SET clockin_pin=:pin WHERE id=:id"),
                {"pin": pin, "id": str(test_users[username]["id"])},
            )
        await db.commit()
    return {
        "organization_id": seed_organization["id"],
        "sv_id": test_users["testsv"]["id"],
        "gm_id": test_users["testgm"]["id"],
    }


# ── find_pin_conflicts ────────────────────────────────────────────────────


async def test_no_conflict_returns_empty(pins_1234_and_5678: dict) -> None:
    async with async_session() as db:
        assert (
            await find_pin_conflicts(db, pins_1234_and_5678["organization_id"], "9012")
            == []
        )


async def test_exact_conflict_is_listed(pins_1234_and_5678: dict) -> None:
    """같은 값을 쓰는 사람만 나온다."""
    async with async_session() as db:
        rows = await find_pin_conflicts(
            db, pins_1234_and_5678["organization_id"], "1234"
        )
    assert [(uid, pin) for uid, pin in rows] == [
        (pins_1234_and_5678["sv_id"], "1234")
    ]


async def test_prefix_overlap_is_not_a_conflict(pins_1234_and_5678: dict) -> None:
    """앞자리가 겹치는 다른 길이 PIN 은 충돌이 아니다 — 양방향 모두.

    기존 `1234` 가 있어도 `123456` 을 쓸 수 있고, 기존 `488528` 이 있어도
    `4885` 를 쓸 수 있다. (키오스크 식별이 정확 일치라 가로채기가 없다.)
    """
    org_id = pins_1234_and_5678["organization_id"]
    async with async_session() as db:
        assert await find_pin_conflicts(db, org_id, "123456") == []
        await db.execute(
            text("UPDATE users SET clockin_pin='488528' WHERE id=:id"),
            {"id": str(pins_1234_and_5678["gm_id"])},
        )
        await db.commit()
        assert await find_pin_conflicts(db, org_id, "4885") == []


async def test_exclude_user_is_ignored(pins_1234_and_5678: dict) -> None:
    """본인 PIN 을 그대로 다시 저장하는 건 충돌이 아니다."""
    async with async_session() as db:
        rows = await find_pin_conflicts(
            db,
            pins_1234_and_5678["organization_id"],
            "1234",
            exclude_user_id=pins_1234_and_5678["sv_id"],
        )
    assert rows == []


async def test_other_org_is_not_scanned(pins_1234_and_5678: dict) -> None:
    async with async_session() as db:
        assert await find_pin_conflicts(db, uuid.uuid4(), "1234") == []


# ── suggest_available_clockin_pin ─────────────────────────────────────────


@pytest.mark.parametrize("length", [4, 5, 6])
async def test_suggestion_never_conflicts(
    pins_1234_and_5678: dict, length: int
) -> None:
    """추천값은 요청 자릿수 + 같은 값 없음 — 그대로 저장 가능해야 의미가 있다."""
    org_id = pins_1234_and_5678["organization_id"]
    async with async_session() as db:
        for _ in range(20):
            pin = await suggest_available_clockin_pin(db, org_id, length=length)
            assert pin is not None
            assert len(pin) == length and pin.isdigit()
            assert await find_pin_conflicts(db, org_id, pin) == []


async def test_suggestion_returns_none_when_space_is_full(
    pins_1234_and_5678: dict,
) -> None:
    """자릿수 공간이 전부 막히면 None — 콘솔이 '자릿수를 늘리세요' 를 띄우는 근거.

    10,000명을 실제로 심을 수는 없으니 DB 조회만 가짜로 바꿔 4자리 값 10,000개가
    전부 쓰이는 상황을 만든다 (후보 검사 자체는 진짜 로직이 돈다).
    """
    org_id = pins_1234_and_5678["organization_id"]

    class _FakeResult:
        @staticmethod
        def scalars():
            class _S:
                @staticmethod
                def all():
                    return [f"{i:04d}" for i in range(10000)]

            return _S()

    class _FakeDB:
        @staticmethod
        async def execute(_stmt):
            return _FakeResult()

    pin = await suggest_available_clockin_pin(_FakeDB(), org_id, length=4)  # type: ignore[arg-type]
    assert pin is None


async def test_suggestion_rejects_bad_length(pins_1234_and_5678: dict) -> None:
    """4~6 밖은 프로그래밍 오류 — 라우터가 422 로 먼저 막지만 서비스도 방어한다."""
    async with async_session() as db:
        with pytest.raises(ValueError):
            await suggest_available_clockin_pin(
                db, pins_1234_and_5678["organization_id"], length=3
            )
