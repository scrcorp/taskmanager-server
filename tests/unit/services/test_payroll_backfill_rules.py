"""Unit — 일별 금액 백필의 순수 규칙 (app/services/payroll_backfill_service.py).

대상 (DB 없음):
    - has_day_amounts: 백필 대상 판별 (일부만 채워져도 대상, 빈 days 는 no-op)
    - mismatch_reason: 동결 스냅샷 == 재계산 결과 검증 게이트 (분/rate/구간/스칼라)
    - patched_breakdown: 금액 4키만 새로 쓰고 나머지 JSON 은 불변

백필은 "고쳐 쓰기"가 아니라 "일치할 때만 채우기"다 — 불일치 케이스가 전부
skip 으로 떨어지는지가 이 파일의 핵심.
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.schemas.payroll import (
    CALC_VERSION,
    ContextDay,
    DayDetail,
    EntryBreakdown,
    PayrollPreviewRow,
    PenaltyLine,
    RateSegment,
)
from app.services.payroll_backfill_service import (
    AMOUNT_KEYS,
    CONTEXT_KEY,
    has_day_amounts,
    mismatch_reason,
    needs_backfill,
    patched_breakdown,
)

_MON = date(2026, 7, 6)
_TUE = date(2026, 7, 7)
_RATE = Decimal("20.00")
_USER = uuid_mod.uuid4()


def _day(
    work_date: date,
    *,
    regular: int = 480,
    ot: int = 0,
    rate: Decimal | None = _RATE,
    amounts: bool,
) -> DayDetail:
    """일별 상세 1건. amounts=False = 금액 필드 생기기 전 동결본 모양."""
    detail = dict(
        work_date=work_date, regular_minutes=regular, ot_minutes=ot,
        applied_rate=rate,
    )
    if not amounts:
        return DayDetail(**detail)
    reg = (Decimal(regular) / 60 * (rate or Decimal("0"))).quantize(Decimal("0.01"))
    ot_amount = (
        Decimal(ot) / 60 * (rate or Decimal("0")) * Decimal("1.5")
    ).quantize(Decimal("0.01"))
    return DayDetail(
        **detail,
        regular_amount=reg,
        ot_amount=ot_amount,
        dt_amount=Decimal("0.00"),
        total_amount=reg + ot_amount,
    )


def _breakdown(*, amounts: bool, days: list[DayDetail] | None = None) -> EntryBreakdown:
    return EntryBreakdown(
        calc_version=CALC_VERSION,
        segments=[
            RateSegment(
                rate=_RATE, regular_minutes=960, ot_minutes=0,
                amount=Decimal("320.00"),
            )
        ],
        days=days
        or [_day(_MON, amounts=amounts), _day(_TUE, amounts=amounts)],
        penalties=[
            PenaltyLine(
                work_date=_MON, kind="meal_penalty",
                reason="Worked 10.0h with no 30-min meal break",
                amount=Decimal("20.00"),
            )
        ],
        tip_period_id=str(uuid_mod.uuid4()),
    )


def _entry(
    *,
    regular_pay: Decimal = Decimal("320.00"),
    ot_pay: Decimal = Decimal("0.00"),
    dt_pay: Decimal = Decimal("0.00"),
) -> SimpleNamespace:
    """PayrollEntry duck-type — 검증이 읽는 필드만."""
    return SimpleNamespace(
        user_id=_USER, member_name="Backfill Tester",
        regular_pay=regular_pay, ot_pay=ot_pay, dt_pay=dt_pay,
    )


def _row(breakdown: EntryBreakdown, **overrides) -> PayrollPreviewRow:
    base = dict(
        user_id=_USER,
        member_name="Backfill Tester",
        regular_minutes=960,
        regular_pay=Decimal("320.00"),
        ot_pay=Decimal("0.00"),
        dt_pay=Decimal("0.00"),
        gross_pay=Decimal("320.00"),
        breakdown=breakdown,
    )
    base.update(overrides)
    return PayrollPreviewRow(**base)


# ---------------------------------------------------------------------------
# has_day_amounts
# ---------------------------------------------------------------------------


def test_legacy_breakdown_is_backfill_target() -> None:
    assert has_day_amounts(_breakdown(amounts=False)) is False


def test_filled_breakdown_is_not_target() -> None:
    assert has_day_amounts(_breakdown(amounts=True)) is True


def test_partially_filled_breakdown_is_target() -> None:
    """한 일자라도 비어 있으면 대상 — 반쪽 상태를 남기지 않는다."""
    mixed = _breakdown(
        amounts=False,
        days=[_day(_MON, amounts=True), _day(_TUE, amounts=False)],
    )
    assert has_day_amounts(mixed) is False


def test_no_days_is_noop() -> None:
    """일자가 없는 entry(근무 0) 는 채울 것이 없다."""
    empty = EntryBreakdown(calc_version=CALC_VERSION, segments=[], days=[])
    assert has_day_amounts(empty) is True


# ---------------------------------------------------------------------------
# needs_backfill — 금액 + context_days (키 유무로 옛 포맷 판별)
# ---------------------------------------------------------------------------


def test_needs_backfill_when_amounts_missing() -> None:
    legacy = _breakdown(amounts=False)
    assert needs_backfill(legacy.model_dump(mode="json"), legacy) is True


def test_needs_backfill_when_context_key_absent() -> None:
    """금액은 있는데 context_days 키가 없는 중간 세대 동결본도 대상."""
    filled = _breakdown(amounts=True)
    raw = filled.model_dump(mode="json")
    raw.pop(CONTEXT_KEY)
    assert needs_backfill(raw, filled) is True


def test_no_backfill_when_both_present_even_if_context_empty() -> None:
    """경계 걸친 주가 없어 빈 목록인 건 정상 — 키가 있으면 대상 아님."""
    filled = _breakdown(amounts=True)
    raw = filled.model_dump(mode="json")
    assert raw[CONTEXT_KEY] == []
    assert needs_backfill(raw, filled) is False


# ---------------------------------------------------------------------------
# mismatch_reason — 일치할 때만 통과
# ---------------------------------------------------------------------------


def test_identical_recompute_passes() -> None:
    frozen = _breakdown(amounts=False)
    live = _breakdown(amounts=True)  # 금액만 다르다 — 그게 백필 대상
    assert mismatch_reason(_entry(), frozen, _row(live)) is None


def test_changed_minutes_are_rejected() -> None:
    frozen = _breakdown(amounts=False)
    live = _breakdown(
        amounts=True,
        days=[_day(_MON, regular=600, amounts=True), _day(_TUE, amounts=True)],
    )
    reason = mismatch_reason(_entry(), frozen, _row(live))
    assert reason is not None
    assert "daily hours" in reason
    assert "confirmed" in reason  # 다음 행동(확정 후 변경 확인) 안내


def test_changed_rate_is_rejected() -> None:
    frozen = _breakdown(amounts=False)
    live = _breakdown(
        amounts=True,
        days=[
            _day(_MON, rate=Decimal("24.00"), amounts=True),
            _day(_TUE, amounts=True),
        ],
    )
    reason = mismatch_reason(_entry(), frozen, _row(live))
    assert reason is not None
    assert "rate" in reason


def test_missing_or_added_work_date_is_rejected_with_dates() -> None:
    frozen = _breakdown(amounts=False)
    live = _breakdown(amounts=True, days=[_day(_MON, amounts=True)])
    reason = mismatch_reason(_entry(), frozen, _row(live))
    assert reason is not None
    assert "work dates" in reason
    assert str(_TUE) in reason  # 어느 날짜가 사라졌는지 사유에 남는다


def test_changed_segments_are_rejected() -> None:
    frozen = _breakdown(amounts=False)
    live = _breakdown(amounts=True)
    live.segments = [
        RateSegment(
            rate=Decimal("24.00"), regular_minutes=960, amount=Decimal("384.00")
        )
    ]
    reason = mismatch_reason(_entry(), frozen, _row(live))
    assert reason is not None
    assert "rate segments" in reason


def test_changed_scalar_pay_is_rejected() -> None:
    """일별/구간이 같아도 스칼라가 다르면 손대지 않는다 (amendment 대상)."""
    frozen = _breakdown(amounts=False)
    live = _breakdown(amounts=True)
    reason = mismatch_reason(
        _entry(regular_pay=Decimal("300.00")), frozen, _row(live)
    )
    assert reason is not None
    assert "pay totals" in reason


# ---------------------------------------------------------------------------
# patched_breakdown — 금액 4키만 교체
# ---------------------------------------------------------------------------


def test_patch_fills_amounts_and_leaves_everything_else() -> None:
    frozen = _breakdown(amounts=False)
    raw = frozen.model_dump(mode="json")
    patched = patched_breakdown(raw, _row(_breakdown(amounts=True)))

    # 금액이 채워졌다 (문자열 직렬화 — 새 confirm 과 같은 표기)
    mon = next(d for d in patched["days"] if d["work_date"] == str(_MON))
    assert mon["regular_amount"] == "160.00"
    assert mon["total_amount"] == "160.00"
    assert all(mon[key] is not None for key in AMOUNT_KEYS)

    # 나머지는 한 글자도 안 바뀐다
    def _without_patched(bd: dict) -> dict:
        clone = {k: v for k, v in bd.items() if k not in ("days", CONTEXT_KEY)}
        clone["days"] = [
            {k: v for k, v in day.items() if k not in AMOUNT_KEYS}
            for day in bd["days"]
        ]
        return clone

    assert _without_patched(patched) == _without_patched(raw)
    assert raw["days"][0]["regular_amount"] is None  # 입력 dict 는 불변 (deepcopy)


def test_patch_writes_context_days_from_recompute() -> None:
    """경계 주 근거도 함께 채운다 — 옛 동결본엔 키 자체가 없던 자리."""
    frozen = _breakdown(amounts=False)
    raw = frozen.model_dump(mode="json")
    raw.pop(CONTEXT_KEY)

    live = _breakdown(amounts=True)
    live.context_days = [
        ContextDay(work_date=date(2026, 7, 4), net_minutes=1200),
        ContextDay(work_date=date(2026, 7, 5), net_minutes=480),
    ]
    patched = patched_breakdown(raw, _row(live))

    assert [c["work_date"] for c in patched[CONTEXT_KEY]] == [
        "2026-07-04", "2026-07-05",
    ]
    assert patched[CONTEXT_KEY][0]["net_minutes"] == 1200
    assert patched[CONTEXT_KEY][0]["paid_in_prior"] is True


def test_patch_writes_empty_context_when_no_straddle() -> None:
    """경계 주가 없으면 빈 목록이라도 키를 남긴다 — 재실행 시 no-op 판정 근거."""
    frozen = _breakdown(amounts=False)
    raw = frozen.model_dump(mode="json")
    raw.pop(CONTEXT_KEY)

    patched = patched_breakdown(raw, _row(_breakdown(amounts=True)))
    assert patched[CONTEXT_KEY] == []
