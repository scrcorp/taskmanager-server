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


async def test_generate_unique_retries_when_first_candidate_conflicts(
    pin_holder: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """첫 후보가 충돌이면 다음 후보로 회피 — claim/confirm 자동배정 경로의 근거.

    (auth_service claim 경로가 `generate_clockin_pin()` 단독 호출에서
    `generate_unique_clockin_pin` 으로 교체된 것의 회귀 방지 — 충돌 후보를
    강제로 먼저 뱉게 해서 재시도가 실제로 동작하는지 본다.)
    """
    import app.services.attendance_device_service as svc

    def _candidates():
        yield "123456"  # pin_holder('1234') 와 prefix 충돌 — 반드시 거부돼야 함
        while True:
            yield "987611"

    gen = _candidates()
    monkeypatch.setattr(svc, "generate_clockin_pin", lambda: next(gen))

    async with async_session() as db:
        pin = await svc.generate_unique_clockin_pin(
            db, pin_holder["organization_id"]
        )
    assert pin == "987611", f"충돌 후보를 회피하지 못함: {pin}"


# ── 409 detail 계약 (pin_conflict) ─────────────────────────────────────


async def _conflict_detail(
    org_id, pin, exclude=None, store_id=None
) -> dict:
    """충돌을 기대하고 409 detail dict 를 돌려받는다."""
    async with async_session() as db:
        with pytest.raises(HTTPException) as exc_info:
            await assert_no_pin_prefix_conflict(
                db, org_id, pin, exclude, store_id=store_id
            )
    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "pin_conflict"
    return detail


async def test_exact_conflict_reports_reason_exact(pin_holder: dict) -> None:
    """제출 PIN == 타인 PIN → reason=exact, store 컨텍스트 없으면 other_store=null."""
    detail = await _conflict_detail(pin_holder["organization_id"], "1234")
    assert detail["reason"] == "exact"
    assert detail["other_store"] is None
    assert detail["message"] == "This PIN is already in use by another employee."


async def test_prefix_conflict_reports_reason_prefix(pin_holder: dict) -> None:
    """길이가 다른 두 PIN 이 앞자리 공유 → reason=prefix."""
    detail = await _conflict_detail(pin_holder["organization_id"], "123456")
    assert detail["reason"] == "prefix"
    assert detail["other_store"] is None
    assert detail["message"] == (
        "This PIN overlaps with another employee's PIN "
        "(numbers that start the same)."
    )


async def test_conflict_detail_never_leaks_pin_or_name(pin_holder: dict) -> None:
    """타인의 PIN 값·이름이 detail 어디에도 실리지 않는다."""
    detail = await _conflict_detail(pin_holder["organization_id"], "123456")
    detail_text = str(detail)
    assert "1234" not in detail_text
    assert "Test SV" not in detail_text  # 충돌 유저(testsv) 의 이름


async def _ensure_user_store(user_id, store_id) -> bool:
    """UserStore 행 보장. 새로 만들었으면 True (호출자가 정리)."""
    from app.models.user_store import UserStore
    from sqlalchemy import select

    async with async_session() as db:
        existing = (
            await db.execute(
                select(UserStore).where(
                    UserStore.user_id == user_id,
                    UserStore.store_id == store_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False
        db.add(UserStore(user_id=user_id, store_id=store_id, is_manager=False))
        await db.commit()
        return True


async def _remove_user_store(user_id, store_id) -> bool:
    """UserStore 행 제거. 있었으면 True (호출자가 복원)."""
    from app.models.user_store import UserStore
    from sqlalchemy import delete

    async with async_session() as db:
        result = await db.execute(
            delete(UserStore).where(
                UserStore.user_id == user_id,
                UserStore.store_id == store_id,
            )
        )
        await db.commit()
        return (result.rowcount or 0) > 0


async def test_store_id_marks_other_store_false_when_conflict_user_in_store(
    pin_holder: dict, test_store_id
) -> None:
    """store_id 지정 + 충돌 유저가 그 매장 소속 → other_store=False."""
    created = await _ensure_user_store(pin_holder["user_id"], test_store_id)
    try:
        detail = await _conflict_detail(
            pin_holder["organization_id"], "1234", store_id=test_store_id
        )
        assert detail["reason"] == "exact"
        assert detail["other_store"] is False
        assert detail["message"] == (
            "This PIN is already in use by another employee."
        )
    finally:
        if created:
            await _remove_user_store(pin_holder["user_id"], test_store_id)


async def test_store_id_marks_other_store_true_when_conflict_user_not_in_store(
    pin_holder: dict, second_store_id
) -> None:
    """store_id 지정 + 충돌 유저가 그 매장에 없음 → other_store=True + 매장 안내."""
    had_row = await _remove_user_store(pin_holder["user_id"], second_store_id)
    try:
        detail = await _conflict_detail(
            pin_holder["organization_id"], "1234", store_id=second_store_id
        )
        assert detail["reason"] == "exact"
        assert detail["other_store"] is True
        assert detail["message"] == (
            "This PIN is already in use by an employee at another store."
        )
    finally:
        if had_row:
            await _ensure_user_store(pin_holder["user_id"], second_store_id)
