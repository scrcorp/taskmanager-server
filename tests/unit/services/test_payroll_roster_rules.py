"""Unit — payroll 로스터 포함 판정 (has_payroll_activity).

대상: app/services/payroll_calc_service.py has_payroll_activity (DB 없음)
    preview_period 가 행 조립 뒤 이 함수로 빈 행을 거른다. 남는 조건 셋
    (지급 분 / 지급액 / 경고)을 각각 단독으로, 그리고 셋 다 없는 빈 행을 본다.
    계정 상태는 판정에 들어가지 않는다 — 행에 그 정보가 없다.
"""

from __future__ import annotations

import uuid as uuid_mod
from decimal import Decimal

import pytest

from app.schemas.payroll import (
    VALIDATION_NO_SHOW,
    VALIDATION_OPEN_SHIFT,
    EntryBreakdown,
    PayrollPreviewRow,
    PreviewValidation,
)
from app.services.payroll_calc_service import has_payroll_activity


def _row(**overrides) -> PayrollPreviewRow:
    """전부 0 인 빈 행 — 테스트가 바꾸고 싶은 필드만 덮어쓴다."""
    user_id = uuid_mod.uuid4()
    base = dict(
        user_id=user_id,
        member_name="Roster Unit",
        breakdown=EntryBreakdown(),
    )
    base.update(overrides)
    return PayrollPreviewRow(**base)


def test_empty_row_has_no_activity() -> None:
    """완결됐는데 net 0분 — 지급 분·액·경고 모두 없으면 목록에서 빠진다."""
    assert has_payroll_activity(_row()) is False


@pytest.mark.parametrize(
    "field", ["regular_minutes", "ot_minutes", "dt_minutes"]
)
def test_any_paid_minutes_keep_row(field: str) -> None:
    """rate 누락으로 금액이 0 이어도 분이 있으면 지급 대상 — 남긴다."""
    assert has_payroll_activity(_row(**{field: 1})) is True


def test_gross_pay_keeps_row() -> None:
    assert has_payroll_activity(_row(gross_pay=Decimal("12.00"))) is True


def test_penalty_only_keeps_row() -> None:
    """판정 보류 일의 잔존 penalty — 근무 0 이어도 지급액이 있다."""
    row = _row(penalty_pay=Decimal("40.00"), gross_pay=Decimal("40.00"))
    assert has_payroll_activity(row) is True


def test_tips_only_keeps_row() -> None:
    row = _row(card_tips=Decimal("15.50"), gross_pay=Decimal("15.50"))
    assert has_payroll_activity(row) is True


def test_negative_tips_offsetting_gross_still_keep_row() -> None:
    """팁 paid-out 이 penalty 와 상쇄돼 gross 0 — 구성 요소가 있으면 남긴다."""
    row = _row(
        penalty_pay=Decimal("20.00"),
        card_tips=Decimal("-20.00"),
        gross_pay=Decimal("0.00"),
    )
    assert has_payroll_activity(row) is True


def test_validation_only_keeps_row() -> None:
    """미퇴근 — 분도 금액도 0 이지만 해결할 경고가 있으면 숨기지 않는다."""
    row = _row(
        validations=[
            PreviewValidation(
                code=VALIDATION_OPEN_SHIFT,
                message="Open shift without clock-out on: 2026-07-06",
                user_id=uuid_mod.uuid4(),
            )
        ]
    )
    assert has_payroll_activity(row) is True


def test_no_show_validation_keeps_row() -> None:
    """no_show 승격 근무 — 결근인지 기록 누락인지 사람이 봐야 하므로 경고 행으로 남긴다."""
    row = _row(
        validations=[
            PreviewValidation(
                code=VALIDATION_NO_SHOW,
                message="Scheduled shift with no clock-in on: 2026-07-06",
                user_id=uuid_mod.uuid4(),
            )
        ]
    )
    assert has_payroll_activity(row) is True
