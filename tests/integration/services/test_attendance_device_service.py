"""Integration tests — attendance_device_service module (DB 사용).

격리: worktree DB 위에서 동작. 테스트 후 변경된 PIN/username 은 fixture (restore_pins) 또는
직접 finally 에서 원복.

[작성됨] — 이번 phase
- commit_pin_or_409
    · 정상 commit (충돌 없음) → 변경된 PIN DB 에 반영
    · uq_user_org_clockin_pin 위반 → HTTPException(409, "Not available")
    · 다른 IntegrityError (uq_user_org_username 등) → 원래 IntegrityError 그대로 raise

[작성 필요] — 추후
- AttendanceDeviceService.register / verify_token / assign_store / unregister
- AttendanceDeviceService.perform_clock_action (clock_in/out, break start/end)
- verify_user_pin

순수 함수 (DB 안 쓰는) 케이스는 tests/unit/services/test_attendance_device_service.py 에.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.attendance_device_service import commit_pin_or_409


@pytest.mark.asyncio
async def test_commit_pin_or_409_succeeds_when_no_conflict(
    db: AsyncSession, test_user: dict, restore_pins,
) -> None:
    """충돌 없을 때 정상 commit — ORM session pending change 가 정상 반영."""
    from app.models.user import User
    from sqlalchemy import select

    new_pin = "111111"
    # 같은 PIN 사용 중인지 확인 후, 충돌 회피용 다른 값으로
    existing = await db.execute(
        select(User.id).where(
            User.organization_id == test_user["organization_id"],
            User.clockin_pin == new_pin,
            User.id != test_user["id"],
        )
    )
    if existing.scalar_one_or_none() is not None:
        new_pin = "111112"

    user = (await db.execute(select(User).where(User.id == test_user["id"]))).scalar_one()
    user.clockin_pin = new_pin
    await commit_pin_or_409(db)

    refreshed = (await db.execute(select(User).where(User.id == test_user["id"]))).scalar_one()
    assert refreshed.clockin_pin == new_pin


@pytest.mark.asyncio
async def test_commit_pin_or_409_raises_409_on_unique_violation(
    db: AsyncSession, test_users: dict, restore_pins,
) -> None:
    """다른 user 와 같은 PIN 으로 commit 시도 → HTTPException(409, 'Not available').

    ORM 세션의 pending change 는 commit 시점에 flush 됨 — commit_pin_or_409 안에서
    IntegrityError 잡힘.
    """
    from app.models.user import User
    from sqlalchemy import select

    target_pin = test_users["testadmin"]["clockin_pin"]
    victim_id = test_users["testgm"]["id"]

    victim = (await db.execute(select(User).where(User.id == victim_id))).scalar_one()
    victim.clockin_pin = target_pin

    with pytest.raises(HTTPException) as exc_info:
        await commit_pin_or_409(db)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Not available"


@pytest.mark.asyncio
async def test_commit_pin_or_409_reraises_other_integrity_error(
    db: AsyncSession, test_users: dict,
) -> None:
    """clockin_pin 외 다른 unique constraint 위반은 그대로 raise (HTTPException 아님).

    uq_user_org_username 위반 유도 — testadmin 의 username 을 testgm 과 같게 ORM 으로 변경.
    """
    from sqlalchemy.exc import IntegrityError
    from app.models.user import User
    from sqlalchemy import select

    testadmin_id = test_users["testadmin"]["id"]
    testadmin = (await db.execute(select(User).where(User.id == testadmin_id))).scalar_one()
    original_username = testadmin.username
    testadmin.username = "testgm"

    try:
        with pytest.raises(IntegrityError):
            await commit_pin_or_409(db)
    finally:
        await db.rollback()
        ta = (await db.execute(select(User).where(User.id == testadmin_id))).scalar_one()
        ta.username = original_username
        await db.commit()
