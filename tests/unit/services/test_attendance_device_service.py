"""Unit tests — attendance_device_service module (mock / no DB).

DB 의존성을 AsyncMock 으로 흉내 — pytest fixture (worktree DB) 와 격리. 빠르고 CI 호환.

[작성됨] — 이번 phase
- generate_clockin_pin (6자리 / zero-pad)
- verify_user_pin (4 분기: 형식 위반 / user 없음 / PIN 불일치 / 정상)
- identify_user_by_pin (5 분기: 형식 위반 / user 없음 / device store None / 정상)

[작성 필요] — 추후
- generate_device_token  (cryptographic 강도, 길이)
- hash_token             (해시 결정성, 다른 입력에 다른 출력)
- generate_device_name   (포맷 'Terminal-XXXX')
- AttendanceDeviceService.register / assign_store / revoke (DB 의존이라 mock 까다로움 — integration 위주가 자연스러움)
- AttendanceDeviceService.perform_clock_action (복합 흐름, mock 보다 integration)
- _compute_today_status_for_user (여러 DB query + setting resolve, mock verbose — integration 으로 커버)

DB 사용하는 케이스는 tests/integration/services/test_attendance_device_service.py 에.

## Mock 패턴 reference

DB 의존 service 함수를 unit test 할 때 사용. AsyncSession 흉내:

```python
from unittest.mock import AsyncMock, MagicMock

db = AsyncMock()
# db.execute() 는 awaitable. 반환값은 sync Result.
result = MagicMock()
result.scalar_one_or_none = MagicMock(return_value=some_user_or_none)
db.execute.return_value = result
```
"""

from __future__ import annotations

import re
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.attendance_device_service import (
    attendance_device_service,
    generate_clockin_pin,
)
from app.utils.exceptions import BadRequestError


# ── generate_clockin_pin (pure function, no mock 필요) ──────────────


def test_generate_clockin_pin_returns_six_digit_string() -> None:
    """6자리 숫자 문자열 반환."""
    pin = generate_clockin_pin()
    assert isinstance(pin, str)
    assert len(pin) == 6
    assert re.fullmatch(r"\d{6}", pin) is not None


def test_generate_clockin_pin_zero_padded() -> None:
    """작은 숫자 (예: 0~999) 도 6자리로 zero-pad. 64회 통계적 검증."""
    for _ in range(64):
        pin = generate_clockin_pin()
        assert len(pin) == 6


# ── verify_user_pin (DB 의존 — AsyncMock 으로 흉내) ────────────────


def _mock_db(scalar_one_or_none_returns) -> AsyncMock:
    """AsyncMock db 를 만들고 db.execute() → result.scalar_one_or_none() chain 을 흉내."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none_returns)
    db.execute.return_value = result
    return db


@pytest.mark.asyncio
async def test_verify_user_pin_rejects_non_digit_pin() -> None:
    """PIN 에 숫자 외 문자 → BadRequestError('PIN must be 6 digits')."""
    db = AsyncMock()  # execute 안 호출됨
    with pytest.raises(BadRequestError, match="PIN must be 6 digits"):
        await attendance_device_service.verify_user_pin(
            db, uuid.uuid4(), "12abcd", uuid.uuid4()
        )
    # 형식 위반은 사전에 거절 — DB query 안 가야 함
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_verify_user_pin_rejects_wrong_length_pin() -> None:
    """PIN 길이가 6 아님 → BadRequestError. (5자리 / 7자리 / 빈 문자열)."""
    db = AsyncMock()
    for bad_pin in ("", "12345", "1234567"):
        with pytest.raises(BadRequestError, match="PIN must be 6 digits"):
            await attendance_device_service.verify_user_pin(
                db, uuid.uuid4(), bad_pin, uuid.uuid4()
            )
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_verify_user_pin_raises_when_user_not_found() -> None:
    """DB 에 user 없음 → BadRequestError('User not found')."""
    db = _mock_db(scalar_one_or_none_returns=None)
    with pytest.raises(BadRequestError, match="User not found"):
        await attendance_device_service.verify_user_pin(
            db, uuid.uuid4(), "123456", uuid.uuid4()
        )
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_user_pin_raises_when_pin_mismatch() -> None:
    """user 는 있는데 PIN 다름 → BadRequestError('Invalid PIN')."""
    user = MagicMock()
    user.clockin_pin = "999999"
    db = _mock_db(scalar_one_or_none_returns=user)
    with pytest.raises(BadRequestError, match="Invalid PIN"):
        await attendance_device_service.verify_user_pin(
            db, uuid.uuid4(), "123456", uuid.uuid4()
        )


@pytest.mark.asyncio
async def test_verify_user_pin_returns_user_on_match() -> None:
    """user 있음 + PIN 일치 → user 객체 반환."""
    user = MagicMock()
    user.clockin_pin = "123456"
    db = _mock_db(scalar_one_or_none_returns=user)
    returned = await attendance_device_service.verify_user_pin(
        db, uuid.uuid4(), "123456", uuid.uuid4()
    )
    assert returned is user


# ── identify_user_by_pin (Phase 3 — DB 의존, AsyncMock) ──────────


def _mock_device(store_id=None) -> MagicMock:
    """AttendanceDevice 흉내 — organization_id, store_id 만 사용."""
    device = MagicMock()
    device.organization_id = uuid.uuid4()
    device.store_id = store_id
    return device


@pytest.mark.asyncio
async def test_identify_user_by_pin_rejects_invalid_format() -> None:
    """PIN 형식 위반 (Stage J: 4~6 자리 외) → BadRequest, DB 조회 skip."""
    db = AsyncMock()
    device = _mock_device()
    for bad_pin in ("", "123", "1234567", "abcdef", "12abcd"):
        with pytest.raises(BadRequestError, match="PIN must be 4-6 digits"):
            await attendance_device_service.identify_user_by_pin(db, bad_pin, device)
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_identify_user_by_pin_raises_when_user_not_found() -> None:
    """PIN 매치되는 user 없음 → BadRequest 'Invalid PIN'."""
    db = _mock_db(scalar_one_or_none_returns=None)
    device = _mock_device()
    with pytest.raises(BadRequestError, match="Invalid PIN"):
        await attendance_device_service.identify_user_by_pin(db, "123456", device)
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_identify_user_by_pin_returns_null_status_when_device_has_no_store() -> None:
    """device.store_id None → IdentifyContext 의 today_status/current_break/scheduled_end 모두 None."""
    user = MagicMock()
    user.id = uuid.uuid4()
    db = _mock_db(scalar_one_or_none_returns=user)
    device = _mock_device(store_id=None)

    ctx = await attendance_device_service.identify_user_by_pin(db, "123456", device)
    assert ctx.user is user
    assert ctx.today_status is None
    assert ctx.current_break is None
    assert ctx.scheduled_end is None
    # _compute_identify_context_for_user 호출되지 않음 → db.execute 1회 (user 조회만)
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_identify_user_by_pin_accepts_4_to_5_digits() -> None:
    """Stage J: 4자리/5자리 PIN 도 형식 통과 (DB 조회 진입)."""
    user = MagicMock()
    user.id = uuid.uuid4()
    db = _mock_db(scalar_one_or_none_returns=user)
    device = _mock_device(store_id=None)
    for pin in ("1234", "12345"):
        db.reset_mock()
        ctx = await attendance_device_service.identify_user_by_pin(db, pin, device)
        assert ctx.user is user
        assert db.execute.await_count == 1


# ── identify_manager_by_pin (Phase 6 — manage 진입 PIN-first) ─────────


@pytest.mark.asyncio
async def test_identify_manager_by_pin_rejects_invalid_format() -> None:
    """PIN 형식 위반 → BadRequest, DB 조회 skip."""
    db = AsyncMock()
    for bad_pin in ("", "123", "1234567", "abcdef", "12abcd"):
        with pytest.raises(BadRequestError, match="PIN must be 4-6 digits"):
            await attendance_device_service.identify_manager_by_pin(db, uuid.uuid4(), bad_pin)
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_identify_manager_by_pin_raises_when_user_not_found() -> None:
    """PIN 매치되는 user 없음 → BadRequest 'Invalid PIN'."""
    db = _mock_db(scalar_one_or_none_returns=None)
    with pytest.raises(BadRequestError, match="Invalid PIN"):
        await attendance_device_service.identify_manager_by_pin(db, uuid.uuid4(), "530025")
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_identify_manager_by_pin_returns_user_on_match() -> None:
    """PIN 매치 → User 반환 (자격 검증은 호출자 책임)."""
    user = MagicMock()
    user.id = uuid.uuid4()
    db = _mock_db(scalar_one_or_none_returns=user)
    out = await attendance_device_service.identify_manager_by_pin(db, uuid.uuid4(), "530025")
    assert out is user
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_identify_manager_by_pin_accepts_4_to_6_digits() -> None:
    """4 / 5 / 6 자리 모두 형식 통과."""
    user = MagicMock()
    db = _mock_db(scalar_one_or_none_returns=user)
    for pin in ("1234", "12345", "123456"):
        db.reset_mock()
        out = await attendance_device_service.identify_manager_by_pin(db, uuid.uuid4(), pin)
        assert out is user
        assert db.execute.await_count == 1
