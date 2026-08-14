"""Unit — clock-in PIN 중복 규칙 (2026-08-13 개정).

규칙은 하나다: **같은 org 안에서 정확히 같은 PIN 만 금지.**
길이가 다르면 앞자리가 겹쳐도(`4885` / `488528`) 서로 다른 PIN 으로 공존한다 —
키오스크 식별이 정확 일치(`clockin_pin == pin`)라 짧은 쪽이 긴 쪽을 가로채지 않는다.

(이 파일은 옛 `test_clockin_pin_prefix.py` 를 대체한다. 그 파일은 prefix 공존을
금지하던 시절의 규칙을 검증했다.)
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text

from app.database import async_session
from app.services.attendance_device_service import (
    assert_no_pin_conflict,
    generate_unique_clockin_pin,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def pin_holder(seed_organization: dict, test_users: dict, restore_pins: None):
    """testsv 의 PIN 을 '1234' 로 고정해두고 그 org_id 와 user_id 를 넘긴다."""
    org_id = seed_organization["id"]
    user_id = test_users["testsv"]["id"]
    async with async_session() as db:
        await db.execute(
            text("UPDATE users SET clockin_pin='1234' WHERE id=:id"),
            {"id": str(user_id)},
        )
        await db.commit()
    return {"organization_id": org_id, "user_id": user_id}


async def _conflicts(org_id, pin, exclude=None) -> bool:
    async with async_session() as db:
        try:
            await assert_no_pin_conflict(db, org_id, pin, exclude)
            return False
        except HTTPException as exc:
            assert exc.status_code == 409
            assert exc.detail["code"] == "pin_conflict"
            assert exc.detail["reason"] == "exact"
            return True


async def test_exact_duplicate_is_rejected(pin_holder: dict) -> None:
    """정확히 같은 PIN — unique 제약과 같은 결론."""
    assert await _conflicts(pin_holder["organization_id"], "1234") is True


async def test_longer_pin_starting_with_existing_is_allowed(pin_holder: dict) -> None:
    """기존 `1234` 가 있어도 `123456` 을 쓸 수 있다 (구 prefix 금지 규칙 제거)."""
    assert await _conflicts(pin_holder["organization_id"], "123456") is False


async def test_shorter_pin_that_starts_existing_is_allowed(
    seed_organization: dict, test_users: dict, restore_pins: None
) -> None:
    """반대 방향도 허용 — 기존 `488528` 이 있어도 `4885` 를 쓸 수 있다."""
    org_id = seed_organization["id"]
    async with async_session() as db:
        await db.execute(
            text("UPDATE users SET clockin_pin='488528' WHERE id=:id"),
            {"id": str(test_users["testsv"]["id"])},
        )
        await db.commit()
    assert await _conflicts(org_id, "4885") is False


async def test_unrelated_pin_is_allowed(pin_holder: dict) -> None:
    assert await _conflicts(pin_holder["organization_id"], "9876") is False


async def test_excluded_user_does_not_conflict_with_itself(pin_holder: dict) -> None:
    """본인 PIN 을 같은 값으로 다시 저장하는 건 충돌이 아니다."""
    assert (
        await _conflicts(
            pin_holder["organization_id"], "1234", pin_holder["user_id"]
        )
        is False
    )


async def test_other_org_pin_does_not_conflict(pin_holder: dict) -> None:
    """PIN unique 범위는 org — 다른 org 의 같은 PIN 은 무관하다."""
    assert await _conflicts(uuid.uuid4(), "1234") is False


async def test_generated_pin_is_six_digits_and_unique(pin_holder: dict) -> None:
    """자동 발급은 6자리 + 기존 값과 정확히 겹치지 않는다.

    `1234` 로 시작하는 6자리(`123456` 등)는 이제 정상 후보다 — 걸러내지 않는다.
    """
    async with async_session() as db:
        for _ in range(20):
            pin = await generate_unique_clockin_pin(
                db, pin_holder["organization_id"]
            )
            assert len(pin) == 6 and pin.isdigit()
            assert pin != "1234"


async def test_generate_unique_retries_when_candidate_is_taken(
    pin_holder: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """첫 후보가 이미 쓰이는 값이면 다음 후보로 회피한다.

    (claim/confirm 자동배정 경로가 `generate_clockin_pin()` 단독 호출에서
    `generate_unique_clockin_pin` 으로 교체된 것의 회귀 방지.)
    """
    org_id = pin_holder["organization_id"]
    taken = "123456"
    async with async_session() as db:
        await db.execute(
            text("UPDATE users SET clockin_pin=:p WHERE id=:id"),
            {"p": taken, "id": str(pin_holder["user_id"])},
        )
        await db.commit()

    candidates = iter([taken, "654321"])
    monkeypatch.setattr(
        "app.services.attendance_device_service.generate_clockin_pin",
        lambda: next(candidates),
    )
    async with async_session() as db:
        assert await generate_unique_clockin_pin(db, org_id) == "654321"
