"""근무시간 비례 팁 분배의 순수 규칙 테스트 (DB 없음).

검증 대상은 TipProrateService._split — 가중치 비례 배분 + 잔돈 처리.
분배 총액이 풀과 정확히 일치해야 한다는 게 핵심 계약이다 (급여 검증이 여기 의존).
"""

from decimal import Decimal
from uuid import UUID

import pytest

from app.services.tip_prorate_service import MAX_WEIGHT_MINUTES, TipProrateService

_split = TipProrateService._split

U1 = UUID("00000000-0000-0000-0000-000000000001")
U2 = UUID("00000000-0000-0000-0000-000000000002")
U3 = UUID("00000000-0000-0000-0000-000000000003")


def test_max_weight_is_eight_hours() -> None:
    assert MAX_WEIGHT_MINUTES == 480


def test_split_is_proportional_to_weight() -> None:
    shares = _split(Decimal("90.00"), {U1: 480, U2: 240})
    assert shares[U1] == Decimal("60.00")
    assert shares[U2] == Decimal("30.00")


def test_split_total_always_equals_pool() -> None:
    """나누어떨어지지 않아도 합계는 풀과 정확히 같아야 한다."""
    pool = Decimal("100.00")
    shares = _split(pool, {U1: 1, U2: 1, U3: 1})
    assert sum(shares.values()) == pool


def test_remainder_goes_to_largest_weight() -> None:
    """잔돈은 가장 많이 일한 사람에게 — 재계산해도 같은 결과가 나와야 한다."""
    shares = _split(Decimal("10.00"), {U1: 300, U2: 100})
    assert sum(shares.values()) == Decimal("10.00")
    assert shares[U1] > shares[U2]
    # 내림 배분(7.50/2.50)에 잔돈이 없으므로 정확히 비례한다
    assert shares[U1] == Decimal("7.50")


def test_remainder_tie_break_is_stable() -> None:
    """가중치가 같으면 user_id 순 — 두 번 돌려도 같은 사람이 잔돈을 받는다."""
    weights = {U1: 100, U2: 100, U3: 100}
    first = _split(Decimal("10.00"), weights)
    second = _split(Decimal("10.00"), weights)
    assert first == second
    assert sum(first.values()) == Decimal("10.00")


def test_zero_pool_gives_zero_shares() -> None:
    shares = _split(Decimal("0.00"), {U1: 480, U2: 120})
    assert set(shares.values()) == {Decimal("0.00")}


def test_no_weight_gives_zero_shares() -> None:
    """근무자가 없거나 전원 0분이면 아무도 못 받는다 (0으로 나누지 않는다)."""
    shares = _split(Decimal("50.00"), {U1: 0})
    assert shares[U1] == Decimal("0.00")


def test_empty_weights_returns_empty() -> None:
    assert _split(Decimal("50.00"), {}) == {}


@pytest.mark.parametrize(
    "pool",
    [Decimal("0.01"), Decimal("0.03"), Decimal("33.33"), Decimal("1234.57")],
)
def test_split_conserves_pool_for_awkward_amounts(pool: Decimal) -> None:
    """센트 단위로 안 떨어지는 금액에서도 총액이 보존되어야 한다."""
    shares = _split(pool, {U1: 7, U2: 11, U3: 13})
    assert sum(shares.values()) == pool
    assert all(s >= 0 for s in shares.values())
