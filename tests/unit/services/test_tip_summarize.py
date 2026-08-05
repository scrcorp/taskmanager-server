"""Unit tests — tip_service.summarize_employee_tips (분배 공식 단일 원천).

DB 없음 — transient ORM 객체로 순수 집계 검증. 4070 폼과 payroll
card_tips_for_period 가 공유하는 공식이므로 방향별 분기 전부 커버:
    - own card / cash 합산 (entry 여러 건)
    - paid_out: status 무관 전액 (pending 포함 — 보낸 쪽 즉시 차감)
    - received_card: accepted/auto_accepted 만 (pending 제외)
    - receiver_id NULL(퇴사) 분배 — 받은 쪽 집계 없음, 보낸 쪽 차감은 유지
    - 대상 밖 entry 의 분배 (ent_by_id 미포함) 는 무시
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.models.tip import TipDistribution, TipEntry
from app.services.tip_service import summarize_employee_tips


def _entry(employee_id, card="0", cash="0") -> TipEntry:
    return TipEntry(
        id=uuid4(),
        store_id=uuid4(),
        employee_id=employee_id,
        date=date(2026, 8, 3),
        card_tips=Decimal(card),
        cash_tips_kept=Decimal(cash),
    )


def _dist(entry: TipEntry, receiver_id, amount: str, status: str) -> TipDistribution:
    return TipDistribution(
        id=uuid4(),
        entry_id=entry.id,
        receiver_id=receiver_id,
        amount=Decimal(amount),
        status=status,
        pending_until=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )


def test_empty_inputs() -> None:
    assert summarize_employee_tips([], []) == {}


def test_own_totals_accumulate_across_entries() -> None:
    emp = uuid4()
    summary = summarize_employee_tips(
        [_entry(emp, card="100.00", cash="20.00"), _entry(emp, card="50.50", cash="5.00")],
        [],
    )
    assert summary[emp]["own_card"] == Decimal("150.50")
    assert summary[emp]["cash"] == Decimal("25.00")
    assert summary[emp]["paid_out"] == Decimal("0")
    assert summary[emp]["received_card"] == Decimal("0")


def test_paid_out_counts_all_statuses_but_received_only_accepted() -> None:
    sender, receiver = uuid4(), uuid4()
    e = _entry(sender, card="100.00")
    summary = summarize_employee_tips(
        [e],
        [
            _dist(e, receiver, "30.00", "accepted"),
            _dist(e, receiver, "10.00", "auto_accepted"),
            _dist(e, receiver, "7.00", "pending"),
        ],
    )
    # 보낸 쪽: pending 포함 전액 차감
    assert summary[sender]["paid_out"] == Decimal("47.00")
    # 받은 쪽: accepted + auto_accepted 만
    assert summary[receiver]["received_card"] == Decimal("40.00")
    assert summary[receiver]["own_card"] == Decimal("0")


def test_null_receiver_still_deducts_sender() -> None:
    sender = uuid4()
    e = _entry(sender, card="60.00")
    summary = summarize_employee_tips([e], [_dist(e, None, "15.00", "accepted")])
    assert summary[sender]["paid_out"] == Decimal("15.00")
    # receiver NULL — 받은 쪽 row 자체가 안 생김
    assert set(summary.keys()) == {sender}


def test_dist_for_unknown_entry_ignored() -> None:
    emp = uuid4()
    e = _entry(emp, card="40.00")
    orphan = _entry(uuid4(), card="99.00")  # entries 목록에 안 넣음
    summary = summarize_employee_tips([e], [_dist(orphan, emp, "20.00", "accepted")])
    assert summary[emp]["paid_out"] == Decimal("0")
    assert summary[emp]["received_card"] == Decimal("0")


def test_pending_only_receiver_absent_from_map() -> None:
    sender, receiver = uuid4(), uuid4()
    e = _entry(sender, card="50.00")
    summary = summarize_employee_tips([e], [_dist(e, receiver, "10.00", "pending")])
    assert receiver not in summary
