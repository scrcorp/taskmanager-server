"""Unit — clock-in PIN prefix 충돌 검사.

PIN 길이가 4~6 로 가변이 되면서 생긴 제약. `uq_user_org_clockin_pin` 은 정확 일치라
`1234` 와 `123456` 의 공존을 막지 못하는데, 이 조합은 6자리 사용자가 앞 4자리만 누르고
확인을 눌렀을 때 **남의 이름으로 출퇴근이 찍히는** 사고로 이어진다.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text

from app.database import async_session
from app.services.attendance_device_service import (
    assert_no_pin_prefix_conflict,
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
            await assert_no_pin_prefix_conflict(db, org_id, pin, exclude)
            return False
        except HTTPException as exc:
            assert exc.status_code == 409
            return True


async def test_exact_duplicate_is_rejected(pin_holder: dict) -> None:
    """정확히 같은 PIN — 기존 unique 제약과 같은 결론."""
    assert await _conflicts(pin_holder["organization_id"], "1234") is True


async def test_new_pin_extending_existing_is_rejected(pin_holder: dict) -> None:
    """기존 `1234` 가 신규 `123456` 의 prefix — 신규 쪽이 오인식 피해자가 된다."""
    assert await _conflicts(pin_holder["organization_id"], "123456") is True


async def test_new_pin_that_is_prefix_of_existing_is_rejected(
    seed_organization: dict, test_users: dict, restore_pins: None
) -> None:
    """신규 `1234` 가 기존 `123456` 의 prefix — 기존 쪽이 오인식 피해자가 된다."""
    org_id = seed_organization["id"]
    async with async_session() as db:
        await db.execute(
            text("UPDATE users SET clockin_pin='123456' WHERE id=:id"),
            {"id": str(test_users["testsv"]["id"])},
        )
        await db.commit()
    assert await _conflicts(org_id, "1234") is True


async def test_unrelated_pin_is_allowed(pin_holder: dict) -> None:
    """앞자리가 겹치지 않으면 통과 — 과잉 차단하지 않는지 확인."""
    assert await _conflicts(pin_holder["organization_id"], "9876") is False


async def test_partial_overlap_that_is_not_a_prefix_is_allowed(
    pin_holder: dict,
) -> None:
    """`1235` 는 `1234` 의 prefix 가 아니다 — 앞 3자리가 같아도 통과."""
    assert await _conflicts(pin_holder["organization_id"], "1235") is False


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


async def test_generated_pin_avoids_prefix_conflict(pin_holder: dict) -> None:
    """자동 발급이 기존 PIN 을 prefix 로 갖는 값을 내놓지 않는다."""
    async with async_session() as db:
        for _ in range(20):
            pin = await generate_unique_clockin_pin(
                db, pin_holder["organization_id"]
            )
            assert len(pin) == 6
            assert not pin.startswith("1234"), f"prefix 충돌 PIN 발급됨: {pin}"
