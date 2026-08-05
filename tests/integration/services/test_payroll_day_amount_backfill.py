"""Integration — 동결 entry 일별 금액 백필 (app/services/payroll_backfill_service.py).

실제 근태로 기간을 확정한 뒤 breakdown 에서 금액 4필드를 지워 "옛 동결본"을
만들고, 백필이 원래 값을 그대로 복원하는지(그리고 그 밖의 바이트는 건드리지
않는지) 확인한다.

    - 백필 → 금액 복원 + 스칼라/구간/penalty/일별 분 불변
    - 확정 후 근태 변조 → skip (사유 기록, breakdown 불변)
    - 이미 금액이 있는 entry → no-op (updated 0)
    - open 기간 → 400 (동결본 없음)
    - payroll_events 불변 — 읽기 전용 재계산 (mutate_events=False)
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.database import async_session
from app.models.attendance import Attendance
from app.models.attendance_break import AttendanceBreak
from app.models.org_member import OrgMember, OrgMemberStore
from app.models.organization import Store
from app.models.payroll import PayPeriod, PayrollEntry, PayrollEvent
from app.models.rate import HourlyRateHistory
from app.models.schedule import Schedule
from app.models.tip import TipEntry, TipPeriod
from app.models.user import User
from app.services.payroll_backfill_service import (
    AMOUNT_KEYS,
    CONTEXT_KEY,
    DAY_WINDOW_KEYS,
    payroll_backfill_service,
)
from app.services.payroll_confirm_service import payroll_confirm_service
from app.services.payroll_period_service import payroll_period_service
from app.utils.exceptions import BadRequestError

_MON = date(2026, 7, 6)
_TUE = date(2026, 7, 7)
_JUL_MID = date(2026, 7, 10)
_RATE = Decimal("20.00")


@pytest_asyncio.fixture
async def backfill_ctx(
    seed_organization: dict, seed_roles: dict[str, UUID], test_users: dict
) -> AsyncIterator[dict]:
    """전용 store + 직원 1명 + Mon 10h / Tue 4h 근태 (확정 가능한 클린 상태)."""
    org_id: UUID = seed_organization["id"]
    suffix = uuid_mod.uuid4().hex[:8]
    async with async_session() as db:
        store = Store(
            organization_id=org_id,
            name=f"__payroll_backfill_store_{suffix}",
            timezone="UTC",
            day_start_time={"all": "00:00"},
            default_hourly_rate=_RATE,
        )
        db.add(store)
        await db.commit()
        await db.refresh(store)

        user = User(
            organization_id=org_id,
            role_id=seed_roles["staff"],
            username=f"__payroll_backfill_{suffix}",
            full_name="Backfill Main",
            password_hash="x",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        member = OrgMember(
            user_id=user.id,
            organization_id=org_id,
            role_id=seed_roles["staff"],
            crewid=900_000 + int(uuid_mod.uuid4().hex[:4], 16),
        )
        db.add(member)
        await db.commit()
        await db.refresh(member)
        db.add(
            OrgMemberStore(
                org_member_id=member.id, store_id=store.id, empid=7
            )
        )
        for work_date, minutes in ((_MON, 600), (_TUE, 240)):
            db.add(
                Attendance(
                    organization_id=org_id,
                    store_id=store.id,
                    user_id=user.id,
                    work_date=work_date,
                    status="clocked_out",
                    total_work_minutes=minutes,
                )
            )
        # 마감 게이트 ④ — 대응 tip period 가 확정돼 있어야 confirm 이 통과한다
        db.add(
            TipPeriod(
                store_id=store.id,
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 15),
                status="confirmed",
            )
        )
        await db.commit()

    ctx = {
        "org_id": org_id,
        "store_id": store.id,
        "user_id": user.id,
        "member_id": member.id,
        "actor": test_users["testadmin"],
    }
    yield ctx

    async with async_session() as db:
        await db.execute(
            delete(PayrollEvent).where(PayrollEvent.store_id == store.id)
        )
        await db.execute(
            delete(PayrollEntry).where(PayrollEntry.store_id == store.id)
        )
        await db.execute(delete(PayPeriod).where(PayPeriod.store_id == store.id))
        await db.execute(delete(TipEntry).where(TipEntry.store_id == store.id))
        await db.execute(delete(TipPeriod).where(TipPeriod.store_id == store.id))
        await db.execute(delete(Attendance).where(Attendance.store_id == store.id))
        await db.execute(delete(Schedule).where(Schedule.store_id == store.id))
        await db.execute(
            delete(HourlyRateHistory).where(
                HourlyRateHistory.org_member_id == member.id
            )
        )
        await db.execute(delete(OrgMember).where(OrgMember.id == member.id))
        await db.execute(delete(User).where(User.id == user.id))
        await db.execute(delete(Store).where(Store.id == store.id))
        await db.commit()


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


async def _confirm_period(ctx: dict) -> UUID:
    """기간 보장 + 확정 → period id (현행 엔진이라 금액까지 동결된다)."""
    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_id=ctx["store_id"], date_in_period=_JUL_MID
        )
        await db.commit()
        period_id = period.id
        actor = await db.get(User, ctx["actor"]["id"])
        await payroll_confirm_service.confirm_period(
            db, store_id=ctx["store_id"], period_id=period_id, actor=actor
        )
        await db.commit()
    return period_id


async def _entry_of(period_id: UUID) -> PayrollEntry:
    async with async_session() as db:
        return (
            await db.execute(
                select(PayrollEntry).where(
                    PayrollEntry.pay_period_id == period_id
                )
            )
        ).scalar_one()


async def _strip_amounts(period_id: UUID) -> dict:
    """동결본에서 선택 필드(금액·근무시각·context_days)를 제거 — 옛 세대 재현.

    Returns:
        지우기 전 breakdown (복원 검증의 기대값)
    """
    async with async_session() as db:
        entry = (
            await db.execute(
                select(PayrollEntry).where(
                    PayrollEntry.pay_period_id == period_id
                )
            )
        ).scalar_one()
        original = entry.breakdown
        legacy = {
            k: v for k, v in original.items() if k not in ("days", CONTEXT_KEY)
        }
        legacy["days"] = [
            {
                k: v
                for k, v in day.items()
                if k not in AMOUNT_KEYS + DAY_WINDOW_KEYS
            }
            for day in original["days"]
        ]
        entry.breakdown = legacy
        await db.commit()
        return original


async def _run_backfill(period_id: UUID) -> dict:
    async with async_session() as db:
        result = await payroll_backfill_service.backfill_frozen_day_amounts(
            db, period_id
        )
        await db.commit()
        return result


async def _events_snapshot(ctx: dict) -> list[tuple]:
    async with async_session() as db:
        events = (
            (
                await db.execute(
                    select(PayrollEvent)
                    .where(PayrollEvent.store_id == ctx["store_id"])
                    .order_by(PayrollEvent.work_date, PayrollEvent.kind)
                )
            )
            .scalars()
            .all()
        )
        return [
            (e.id, e.kind, e.work_date, e.voided_at, e.pay_period_id)
            for e in events
        ]


# ---------------------------------------------------------------------------
# 백필 성공 경로
# ---------------------------------------------------------------------------


async def test_backfill_restores_day_amounts(backfill_ctx: dict) -> None:
    """옛 동결본 → 재계산으로 금액 복원. 금액 외 바이트는 그대로.

    Mon 10h = reg $160.00 + OT $60.00 → $220.00 / Tue 4h = $80.00.
    """
    period_id = await _confirm_period(backfill_ctx)
    original = await _strip_amounts(period_id)
    events_before = await _events_snapshot(backfill_ctx)

    result = await _run_backfill(period_id)

    assert result["updated"] == 1
    assert result["skipped"] == []
    assert result["unchanged"] == 0

    entry = await _entry_of(period_id)
    days = {d["work_date"]: d for d in entry.breakdown["days"]}
    assert days[_MON.isoformat()]["regular_amount"] == "160.00"
    assert days[_MON.isoformat()]["ot_amount"] == "60.00"
    assert days[_MON.isoformat()]["total_amount"] == "220.00"
    assert days[_TUE.isoformat()]["total_amount"] == "80.00"

    # 동결본 복원 — 확정 당시 breakdown 과 완전히 동일
    assert entry.breakdown == original
    # 스칼라 컬럼은 손대지 않는다
    assert entry.regular_pay == Decimal("240.00")
    assert entry.ot_pay == Decimal("60.00")
    # 읽기 전용 재계산 — 동결 이벤트 불변 (id/void/기간 스탬프 전부)
    assert await _events_snapshot(backfill_ctx) == events_before


async def test_backfill_restores_worked_times(backfill_ctx: dict) -> None:
    """근무/휴게 벽시계도 함께 복원된다 — 옛 동결본엔 키 자체가 없던 자리."""
    async with async_session() as db:
        att = (
            await db.execute(
                select(Attendance).where(
                    Attendance.store_id == backfill_ctx["store_id"],
                    Attendance.work_date == _MON,
                )
            )
        ).scalar_one()
        att.clock_in = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)
        att.clock_out = datetime(2026, 7, 6, 18, 30, tzinfo=timezone.utc)
        db.add(
            AttendanceBreak(
                attendance_id=att.id, break_type="unpaid_meal",
                started_at=datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
                ended_at=datetime(2026, 7, 6, 12, 30, tzinfo=timezone.utc),
                duration_minutes=30,
            )
        )
        await db.commit()

    period_id = await _confirm_period(backfill_ctx)
    original = await _strip_amounts(period_id)
    stripped = (await _entry_of(period_id)).breakdown
    assert "shifts" not in stripped["days"][0]

    result = await _run_backfill(period_id)
    assert result["updated"] == 1

    entry = await _entry_of(period_id)
    mon = next(d for d in entry.breakdown["days"] if d["work_date"] == _MON.isoformat())
    assert mon["shifts"] == [{"start": "08:00", "end": "18:30"}]
    assert mon["breaks"] == [
        {"start": "12:00", "end": "12:30", "type": "unpaid_meal"}
    ]
    assert entry.breakdown == original  # 확정 당시 동결본과 완전 일치


async def test_backfill_is_idempotent_and_skips_filled_entries(
    backfill_ctx: dict
) -> None:
    """이미 금액이 있으면 no-op — 재실행해도 아무것도 쓰지 않는다."""
    period_id = await _confirm_period(backfill_ctx)

    first = await _run_backfill(period_id)  # 현행 엔진이라 이미 금액 있음
    assert first["updated"] == 0
    assert first["unchanged"] == 1
    assert first["skipped"] == []

    await _strip_amounts(period_id)
    filled = await _run_backfill(period_id)
    assert filled["updated"] == 1

    entry_after_first = (await _entry_of(period_id)).breakdown
    again = await _run_backfill(period_id)
    assert again["updated"] == 0
    assert again["unchanged"] == 1
    assert (await _entry_of(period_id)).breakdown == entry_after_first


async def test_backfill_restores_context_days_for_straddle_week(
    backfill_ctx: dict
) -> None:
    """경계 걸친 주 — 직전 기간 일자가 context_days 로 복원된다 (지급은 미포함).

    7/12~15(전기, 각 8h = 32h) + 7/16 8h → 주 straight 40h 도달, 7/17 6h 는
    전부 weekly OT. 기간 B 에 지급되는 일자는 16·17 뿐이고, 12~15 는
    "이 주가 이미 40h였다"의 근거로만 남는다.
    """
    org_id, store_id, user_id = (
        backfill_ctx["org_id"], backfill_ctx["store_id"], backfill_ctx["user_id"]
    )
    prior_days = [
        date(2026, 7, 12), date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15),
    ]
    async with async_session() as db:
        for work_date, minutes in (
            [(d, 480) for d in prior_days] + [(date(2026, 7, 16), 480),
                                              (date(2026, 7, 17), 360)]
        ):
            db.add(
                Attendance(
                    organization_id=org_id, store_id=store_id, user_id=user_id,
                    work_date=work_date, status="clocked_out",
                    total_work_minutes=minutes,
                )
            )
        db.add(
            TipPeriod(
                store_id=store_id, start_date=date(2026, 7, 16),
                end_date=date(2026, 7, 31), status="confirmed",
            )
        )
        await db.commit()

    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_id=store_id, date_in_period=date(2026, 7, 20)
        )
        await db.commit()
        period_id = period.id
        actor = await db.get(User, backfill_ctx["actor"]["id"])
        await payroll_confirm_service.confirm_period(
            db, store_id=store_id, period_id=period_id, actor=actor
        )
        await db.commit()

    original = await _strip_amounts(period_id)
    assert CONTEXT_KEY not in (await _entry_of(period_id)).breakdown

    result = await _run_backfill(period_id)
    assert result["updated"] == 1
    assert result["skipped"] == []

    entry = await _entry_of(period_id)
    assert [c["work_date"] for c in entry.breakdown[CONTEXT_KEY]] == [
        d.isoformat() for d in prior_days
    ]
    assert {c["net_minutes"] for c in entry.breakdown[CONTEXT_KEY]} == {480}
    assert all(c["paid_in_prior"] for c in entry.breakdown[CONTEXT_KEY])
    # 지급 일자는 여전히 기간 내 2일뿐 — 근거가 지급으로 새지 않는다
    assert [d["work_date"] for d in entry.breakdown["days"]] == [
        "2026-07-16", "2026-07-17",
    ]
    assert entry.breakdown == original  # 확정 당시 동결본과 완전 일치
    assert entry.regular_pay == Decimal("160.00")  # 7/16 8h × $20 (40h 딱 도달)
    assert entry.ot_pay == Decimal("180.00")  # 7/17 6h × $20 × 1.5 — 40h 초과분


# ---------------------------------------------------------------------------
# 안전 게이트 — 확정 후 원본이 바뀐 경우
# ---------------------------------------------------------------------------


async def test_backfill_skips_entry_when_attendance_changed(
    backfill_ctx: dict
) -> None:
    """확정 후 근태가 바뀐 기간은 건드리지 않는다 — skip + 사유, breakdown 불변."""
    period_id = await _confirm_period(backfill_ctx)
    await _strip_amounts(period_id)
    before = (await _entry_of(period_id)).breakdown  # 금액 없는 상태 (기대: 불변)

    async with async_session() as db:
        att = (
            await db.execute(
                select(Attendance).where(
                    Attendance.store_id == backfill_ctx["store_id"],
                    Attendance.work_date == _MON,
                )
            )
        ).scalar_one()
        att.total_work_minutes = 300  # 10h → 5h 로 변조
        await db.commit()

    result = await _run_backfill(period_id)

    assert result["updated"] == 0
    assert len(result["skipped"]) == 1
    skipped = result["skipped"][0]
    assert skipped["user_id"] == str(backfill_ctx["user_id"])
    assert "confirmed" in skipped["reason"]  # 확정 후 변경됐다는 안내

    # 동결본은 한 글자도 안 바뀐다 — 금액도 여전히 비어 있다
    entry = await _entry_of(period_id)
    assert entry.breakdown == before
    assert "total_amount" not in entry.breakdown["days"][0]


async def test_backfill_skips_when_work_date_disappears(
    backfill_ctx: dict
) -> None:
    """근태 행 자체가 삭제된 경우 — 사라진 날짜를 사유에 남기고 skip."""
    period_id = await _confirm_period(backfill_ctx)
    await _strip_amounts(period_id)

    async with async_session() as db:
        await db.execute(
            delete(Attendance).where(
                Attendance.store_id == backfill_ctx["store_id"],
                Attendance.work_date == _TUE,
            )
        )
        await db.commit()

    result = await _run_backfill(period_id)

    assert result["updated"] == 0
    assert len(result["skipped"]) == 1
    assert "work dates" in result["skipped"][0]["reason"]
    assert str(_TUE) in result["skipped"][0]["reason"]


async def test_backfill_skips_when_rate_history_changed(
    backfill_ctx: dict
) -> None:
    """확정 후 시급 이력이 바뀌면 금액이 달라지므로 skip (구간 불일치)."""
    period_id = await _confirm_period(backfill_ctx)
    await _strip_amounts(period_id)

    async with async_session() as db:
        db.add(
            HourlyRateHistory(
                organization_id=backfill_ctx["org_id"],
                org_member_id=backfill_ctx["member_id"],
                new_rate=Decimal("30.00"),
                effective_date=_MON - timedelta(days=7),
                reason="__retroactive__",
            )
        )
        await db.commit()

    result = await _run_backfill(period_id)

    assert result["updated"] == 0
    assert len(result["skipped"]) == 1
    assert "rate" in result["skipped"][0]["reason"]


# ---------------------------------------------------------------------------
# 대상 기간 검증
# ---------------------------------------------------------------------------


async def test_backfill_rejects_open_period(backfill_ctx: dict) -> None:
    """미확정 기간은 동결본이 없다 — 400 (preview 가 원천)."""
    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_id=backfill_ctx["store_id"], date_in_period=_JUL_MID
        )
        await db.commit()
        period_id = period.id

    async with async_session() as db:
        with pytest.raises(BadRequestError, match="not confirmed"):
            await payroll_backfill_service.backfill_frozen_day_amounts(
                db, period_id
            )


async def test_backfill_all_confirmed_covers_store_periods(
    backfill_ctx: dict
) -> None:
    """--all-confirmed 경로 — store 한정으로 확정 기간을 훑는다."""
    period_id = await _confirm_period(backfill_ctx)
    await _strip_amounts(period_id)

    async with async_session() as db:
        results = await payroll_backfill_service.backfill_all_confirmed(
            db, store_id=backfill_ctx["store_id"]
        )
        await db.commit()

    assert [r["period_id"] for r in results] == [str(period_id)]
    assert results[0]["updated"] == 1
    assert (await _entry_of(period_id)).breakdown["days"][0][
        "total_amount"
    ] == "220.00"
