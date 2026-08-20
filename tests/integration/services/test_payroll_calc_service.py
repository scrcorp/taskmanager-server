"""Integration — payroll_calc_service 골든 테스트 (Payroll v1 Phase 2 핵심).

대상: app/services/payroll_calc_service.py preview_period
    실제 attendance/rate/tip 픽스처를 만들고 손계산 기대값과 비교한다.

    (a) OT 없는 단순 주            (b) 일별 OT 10h          (c) DT 13h
    (d) 주간 OT 42h                (e) 경계 걸친 주 — live 전기 (A2)
    (e2) 경계 걸친 주 — frozen 전기 + 7일 연속   (f) 기간 중간 인상 가중평균
    (g) 7일 연속                   (h) 같은 날 split shift 합산
    (i) meal+rest penalty 금액     (j) validations 5종
    (k) 일별 금액 (days[].regular/ot/dt/total_amount — 표시용 정보값)
    + 멱등/결정성, 판정 보류 일 penalty rate 재조회

스펙: docs/99_inbox/2026-08-03 payroll-v1-스키마-스펙.md §5·계산 규칙 1~5,
설계방향 C1~C8. 서비스는 commit 하지 않으므로 테스트가 commit 한다.
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import AsyncIterator
from uuid import UUID

import pytest_asyncio
from sqlalchemy import delete, select

from app.core.payroll_rules import (
    EVENT_KIND_DAILY_OT,
    EVENT_KIND_MEAL_PENALTY,
    EVENT_KIND_REST_PENALTY,
    EVENT_KIND_SEVENTH_DAY,
    EVENT_KIND_WEEKLY_OT,
)
from app.database import async_session
from app.models.attendance import Attendance
from app.models.attendance_break import AttendanceBreak
from app.models.org_member import OrgMember, OrgMemberStore
from app.models.organization import Store, StoreGroup
from app.models.settings import StoreSetting
from app.seeds.settings_seed import REST_BREAK_TRACKING_KEY
from app.models.payroll import PayPeriod, PayrollEntry, PayrollEvent
from app.models.rate import HourlyRateHistory
from app.models.schedule import Schedule
from app.models.tip import TipEntry, TipPeriod
from app.models.user import User
from app.schemas.payroll import (
    CALC_VERSION,
    VALIDATION_BELOW_MINIMUM_WAGE,
    VALIDATION_NO_SHOW,
    VALIDATION_OPEN_SHIFT,
    VALIDATION_RATE_MISSING,
    VALIDATION_TIP_PERIOD_NOT_CONFIRMED,
    VALIDATION_UNCONFIRMED_AUTO_CLOCKOUT,
    DayDetail,
    EntryBreakdown,
    PayrollPreviewRow,
)
from app.services.payroll_calc_service import payroll_calc_service
from app.services.payroll_period_service import payroll_period_service

# 기준 주 — 2026-07-05(일) ~ 07-11(토), 기간 2026-07-01 ~ 07-15 안에 통째로 든다.
_SUN = date(2026, 7, 5)
_MON = date(2026, 7, 6)
_TUE = date(2026, 7, 7)
_WED = date(2026, 7, 8)
_FRI = date(2026, 7, 10)
_SAT = date(2026, 7, 11)
_JUL_MID = date(2026, 7, 10)  # 기간 지정용 (7/1~15)

_RATE = Decimal("20.00")  # store default — 픽스처 기본 시급


# ---------------------------------------------------------------------------
# 픽스처 — throwaway store + user/member (종료 시 전부 정리)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def calc_ctx(
    seed_organization: dict, seed_roles: dict[str, UUID]
) -> AsyncIterator[dict]:
    """전용 store + 기본 user/member (store default 20.00, member rate NULL)."""
    org_id: UUID = seed_organization["id"]
    suffix = uuid_mod.uuid4().hex[:8]
    async with async_session() as db:
        group = StoreGroup(organization_id=org_id, name=f"__payroll_calc_group_{suffix}")
        db.add(group)
        await db.flush()
        store = Store(
            organization_id=org_id,
            group_id=group.id,
            name=f"__payroll_calc_store_{suffix}",
            timezone="UTC",
            day_start_time={"all": "00:00"},
            default_hourly_rate=_RATE,
        )
        db.add(store)
        await db.commit()
        await db.refresh(store)
        await db.refresh(group)
        # rest penalty 는 "휴게를 기록하는 매장"에서만 판정한다(기본 off).
        # 이 테스트들은 penalty 금액을 검증하므로 켜 둔다.
        db.add(
            StoreSetting(
                store_id=store.id,
                key=REST_BREAK_TRACKING_KEY,
                value=True,
            )
        )
        await db.commit()
    ctx = {
        "org_id": org_id,
        "group_id": group.id,
        "store_id": store.id,
        "role_id": seed_roles["staff"],
        "user_ids": [],
        "member_ids": [],
    }
    # crewid 는 org 내 unique — 공유 seed org 라 충돌 없는 높은 난수 사용
    crewid = 900_000 + int(uuid_mod.uuid4().hex[:4], 16)
    main = await _mk_user(ctx, "Calc Main", crewid=crewid, empid=7)
    ctx["user_id"] = main["user_id"]
    ctx["member_id"] = main["member_id"]
    ctx["crewid"] = crewid

    yield ctx

    async with async_session() as db:
        store_id = ctx["store_id"]
        await db.execute(delete(PayrollEvent).where(PayrollEvent.store_id == store_id))
        await db.execute(
            delete(PayrollEntry).where(
                PayrollEntry.pay_period_id.in_(
                    select(PayPeriod.id).where(
                        PayPeriod.store_group_id == ctx["group_id"]
                    )
                )
            )
        )
        await db.execute(
            delete(PayPeriod).where(PayPeriod.store_group_id == ctx["group_id"])
        )
        await db.execute(delete(PayPeriod).where(PayPeriod.store_id == store_id))
        await db.execute(delete(TipEntry).where(TipEntry.store_id == store_id))
        await db.execute(delete(TipPeriod).where(TipPeriod.store_id == store_id))
        # attendance_breaks / org_member_stores 는 FK CASCADE 로 같이 삭제
        await db.execute(delete(Attendance).where(Attendance.store_id == store_id))
        await db.execute(delete(Schedule).where(Schedule.store_id == store_id))
        await db.execute(
            delete(HourlyRateHistory).where(
                HourlyRateHistory.org_member_id.in_(ctx["member_ids"])
            )
        )
        await db.execute(
            delete(OrgMember).where(OrgMember.id.in_(ctx["member_ids"]))
        )
        await db.execute(delete(User).where(User.id.in_(ctx["user_ids"])))
        await db.execute(delete(Store).where(Store.id == store_id))
        await db.execute(delete(StoreGroup).where(StoreGroup.id == ctx["group_id"]))
        await db.commit()


# ---------------------------------------------------------------------------
# 헬퍼 — user/attendance/schedule/rate/preview
# ---------------------------------------------------------------------------


async def _mk_user(
    ctx: dict,
    full_name: str,
    *,
    member_rate: Decimal | None = None,
    crewid: int | None = None,
    empid: int | None = None,
) -> dict:
    """user + org_member (+ 매장배정/empid) 생성. ctx 정리 목록에 등록."""
    suffix = uuid_mod.uuid4().hex[:8]
    async with async_session() as db:
        user = User(
            organization_id=ctx["org_id"],
            role_id=ctx["role_id"],
            username=f"__payroll_calc_{suffix}",
            full_name=full_name,
            password_hash="x",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        member = OrgMember(
            user_id=user.id,
            organization_id=ctx["org_id"],
            role_id=ctx["role_id"],
            hourly_rate=member_rate,
            crewid=crewid,
        )
        db.add(member)
        await db.commit()
        await db.refresh(member)
        if empid is not None:
            db.add(
                OrgMemberStore(
                    org_member_id=member.id, store_id=ctx["store_id"], empid=empid
                )
            )
            await db.commit()
    ctx["user_ids"].append(user.id)
    ctx["member_ids"].append(member.id)
    return {"user_id": user.id, "member_id": member.id}


async def _mk_attendance(
    ctx: dict,
    *,
    user_id: UUID | None = None,
    work_date: date,
    total_work_minutes: int | None,
    status: str = "clocked_out",
    schedule_id: UUID | None = None,
    clock_in: datetime | None = None,
    clock_out: datetime | None = None,
    anomalies: list[str] | None = None,
) -> UUID:
    async with async_session() as db:
        att = Attendance(
            organization_id=ctx["org_id"],
            store_id=ctx["store_id"],
            user_id=user_id or ctx["user_id"],
            schedule_id=schedule_id,
            work_date=work_date,
            status=status,
            total_work_minutes=total_work_minutes,
            clock_in=clock_in,
            clock_out=clock_out,
            anomalies=anomalies,
        )
        db.add(att)
        await db.commit()
        await db.refresh(att)
        return att.id


async def _mk_schedule(ctx: dict, *, work_date: date, user_id: UUID | None = None) -> UUID:
    """split shift 용 스케줄 (walk-in unique 회피)."""
    async with async_session() as db:
        sched = Schedule(
            organization_id=ctx["org_id"],
            user_id=user_id or ctx["user_id"],
            store_id=ctx["store_id"],
            operating_day=work_date,
            start_at=datetime.combine(work_date, datetime.min.time()),
            end_at=datetime.combine(work_date, datetime.min.time()) + timedelta(hours=5),
            status="confirmed",
        )
        db.add(sched)
        await db.commit()
        await db.refresh(sched)
        return sched.id


async def _add_rate_history(
    ctx: dict, *, member_id: UUID | None = None, new_rate: str, effective: date
) -> None:
    async with async_session() as db:
        db.add(
            HourlyRateHistory(
                organization_id=ctx["org_id"],
                org_member_id=member_id or ctx["member_id"],
                new_rate=Decimal(new_rate),
                effective_date=effective,
                reason="__test__",
            )
        )
        await db.commit()


async def _preview(ctx: dict, date_in_period: date) -> list[PayrollPreviewRow]:
    """ensure_period + preview 실행 후 commit (호출자 트랜잭션 소유 관례)."""
    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_group_id=ctx["group_id"], date_in_period=date_in_period
        )
        rows = await payroll_calc_service.preview_period(db, period)
        await db.commit()
        return rows


def _row_for(rows: list[PayrollPreviewRow], user_id: UUID) -> PayrollPreviewRow:
    matches = [r for r in rows if r.user_id == user_id]
    assert len(matches) == 1, f"expected 1 row for user, got {len(matches)}"
    return matches[0]


def _codes(row: PayrollPreviewRow) -> set[str]:
    return {v.code for v in row.validations}


async def _events(ctx: dict, kind: str | None = None) -> list[PayrollEvent]:
    async with async_session() as db:
        query = select(PayrollEvent).where(
            PayrollEvent.store_id == ctx["store_id"],
            PayrollEvent.voided_at.is_(None),
        )
        if kind is not None:
            query = query.where(PayrollEvent.kind == kind)
        return list(
            (await db.execute(query.order_by(PayrollEvent.work_date))).scalars().all()
        )


# ---------------------------------------------------------------------------
# (a) 단순 주 — OT 없음
# ---------------------------------------------------------------------------


async def test_simple_week_no_overtime(calc_ctx: dict) -> None:
    """월~금 3h×5 = 15h 전부 regular (penalty 임계 3.5h 미만 — 완전 클린 주).

    손계산: 15h × $20 = $300.00. 이벤트/penalty 전혀 없음.
    """
    for i in range(5):
        await _mk_attendance(calc_ctx, work_date=_MON + timedelta(days=i), total_work_minutes=180)

    rows = await _preview(calc_ctx, _JUL_MID)
    row = _row_for(rows, calc_ctx["user_id"])

    assert (row.regular_minutes, row.ot_minutes, row.dt_minutes) == (900, 0, 0)
    assert row.regular_pay == Decimal("300.00")
    assert row.ot_pay == Decimal("0.00")
    assert row.dt_pay == Decimal("0.00")
    assert row.penalty_pay == Decimal("0.00")
    assert row.card_tips == Decimal("0.00")
    assert row.gross_pay == Decimal("300.00")
    assert row.member_name == "Calc Main"
    assert row.empid == 7
    assert row.crewid == calc_ctx["crewid"]
    assert row.validations == []
    assert row.breakdown.calc_version == CALC_VERSION
    assert row.breakdown.penalties == []
    assert len(row.breakdown.days) == 5
    assert [s.rate for s in row.breakdown.segments] == [Decimal("20.00")]
    assert row.breakdown.segments[0].regular_minutes == 900
    assert row.breakdown.segments[0].amount == Decimal("300.00")
    assert await _events(calc_ctx) == []  # 이벤트 전혀 없음


# ---------------------------------------------------------------------------
# (b) 일별 OT — 10h 하루
# ---------------------------------------------------------------------------


async def test_daily_overtime_10h_day(calc_ctx: dict) -> None:
    """10h 하루 = 8h reg + 2h OT.

    손계산: reg 8h×$20 = $160.00, OT 2h×1.5×$20 = $60.00 → gross $220.00 + penalty.
    (10h 무휴게 → meal+rest penalty 2h×$20 = $40.00)
    """
    att_id = await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=600)

    rows = await _preview(calc_ctx, _JUL_MID)
    row = _row_for(rows, calc_ctx["user_id"])

    assert (row.regular_minutes, row.ot_minutes, row.dt_minutes) == (480, 120, 0)
    assert row.regular_pay == Decimal("160.00")
    assert row.ot_pay == Decimal("60.00")
    assert row.dt_pay == Decimal("0.00")
    assert row.penalty_pay == Decimal("40.00")  # meal $20 + rest $20
    assert row.gross_pay == Decimal("260.00")

    daily = await _events(calc_ctx, EVENT_KIND_DAILY_OT)
    assert len(daily) == 1
    assert daily[0].reason == "Daily overtime: 2.0h over 8h/day at 1.5x"
    assert daily[0].attendance_id == att_id  # 1:1 링크
    assert daily[0].work_date == _MON


# ---------------------------------------------------------------------------
# (c) DT — 13h 하루
# ---------------------------------------------------------------------------


async def test_double_time_13h_day(calc_ctx: dict) -> None:
    """13h 하루 = 8h reg + 4h OT + 1h DT.

    손계산: reg $160.00 + OT 4×1.5×20=$120.00 + DT 1×2×20=$40.00 = $320.00
    + penalty $40.00 (무휴게) → gross $360.00.
    """
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=780)

    rows = await _preview(calc_ctx, _JUL_MID)
    row = _row_for(rows, calc_ctx["user_id"])

    assert (row.regular_minutes, row.ot_minutes, row.dt_minutes) == (480, 240, 60)
    assert row.regular_pay == Decimal("160.00")
    assert row.ot_pay == Decimal("120.00")
    assert row.dt_pay == Decimal("40.00")
    assert row.gross_pay == Decimal("360.00")

    daily = await _events(calc_ctx, EVENT_KIND_DAILY_OT)
    assert len(daily) == 1
    assert daily[0].reason == (
        "Daily overtime: 4.0h over 8h/day at 1.5x, 1.0h over 12h/day at 2x"
    )
    seg = row.breakdown.segments[0]
    assert (seg.regular_minutes, seg.ot_minutes, seg.dt_minutes) == (480, 240, 60)
    assert seg.amount == Decimal("320.00")


# ---------------------------------------------------------------------------
# (d) 주간 OT — 6×7h = 42h
# ---------------------------------------------------------------------------


async def test_weekly_overtime_42h(calc_ctx: dict) -> None:
    """일~금 7h×6 = 42h → 마지막 날(금)에 2h weekly OT.

    손계산: reg 40h×$20 = $800.00, OT 2h×1.5×$20 = $60.00.
    7h 무휴게 일 6개 → penalty 12h... 아님: meal+rest 각 1h × 6일 = $240.00.
    """
    for i in range(6):
        await _mk_attendance(calc_ctx, work_date=_SUN + timedelta(days=i), total_work_minutes=420)

    rows = await _preview(calc_ctx, _JUL_MID)
    row = _row_for(rows, calc_ctx["user_id"])

    assert (row.regular_minutes, row.ot_minutes, row.dt_minutes) == (2400, 120, 0)
    assert row.regular_pay == Decimal("800.00")
    assert row.ot_pay == Decimal("60.00")
    assert row.penalty_pay == Decimal("240.00")  # 6일 × (meal 1h + rest 1h) × $20
    assert row.gross_pay == Decimal("1100.00")

    weekly = await _events(calc_ctx, EVENT_KIND_WEEKLY_OT)
    assert len(weekly) == 1
    assert weekly[0].work_date == _FRI
    assert weekly[0].reason == "Weekly overtime: 2.0h over 40h/week at 1.5x"
    # 금요일 상세: 5h reg + 2h weekly OT
    fri = [d for d in row.breakdown.days if d.work_date == _FRI][0]
    assert (fri.regular_minutes, fri.ot_minutes) == (300, 120)


# ---------------------------------------------------------------------------
# (e) 경계 걸친 주 — live 전기 (A2 예시)
# ---------------------------------------------------------------------------


async def test_straddle_week_live_prior_period(calc_ctx: dict) -> None:
    """기간이 금 10/16 에 시작. 같은 주 일~목(10/11~15, 전기)에 이미 40h.

    분류: 전기 시간 합산으로 금요일 8h 는 전부 weekly OT (C4).
    지급: 현 기간 근무분(금)만 — 손계산: 8h×1.5×$20 = $240.00.
    전기 pay period 행이 없으므로(un-confirmed) live attendance 가 분류 원천.
    """
    for i in range(5):  # 일 10/11 ~ 목 10/15 — 전기 (10/1~15)
        await _mk_attendance(
            calc_ctx, work_date=date(2026, 10, 11) + timedelta(days=i),
            total_work_minutes=480,
        )
    await _mk_attendance(calc_ctx, work_date=date(2026, 10, 16), total_work_minutes=480)

    rows = await _preview(calc_ctx, date(2026, 10, 20))  # 기간 10/16~31
    row = _row_for(rows, calc_ctx["user_id"])

    # 지급은 금요일 하루만 — 전기 일자는 days 에 없다
    assert [d.work_date for d in row.breakdown.days] == [date(2026, 10, 16)]
    # 그 대신 40h 판정 근거로 context_days 에 남는다 (지급 아님)
    assert [c.work_date for c in row.breakdown.context_days] == [
        date(2026, 10, 11) + timedelta(days=i) for i in range(5)
    ]
    assert {c.net_minutes for c in row.breakdown.context_days} == {480}
    assert all(c.paid_in_prior for c in row.breakdown.context_days)
    assert (row.regular_minutes, row.ot_minutes, row.dt_minutes) == (0, 480, 0)
    assert row.regular_pay == Decimal("0.00")
    assert row.ot_pay == Decimal("240.00")
    assert row.penalty_pay == Decimal("40.00")  # 금 8h 무휴게 — meal+rest ($20 각)
    assert row.gross_pay == Decimal("280.00")

    weekly = await _events(calc_ctx, EVENT_KIND_WEEKLY_OT)
    assert [e.work_date for e in weekly] == [date(2026, 10, 16)]
    assert await _events(calc_ctx, EVENT_KIND_DAILY_OT) == []
    # 전기 일자 이벤트는 이 기간 범위 밖 — 생성되지 않는다
    assert row.breakdown.sources == {
        "prior_period_ids": [],
        "prior_period_frozen": False,
    }


# ---------------------------------------------------------------------------
# (e2) 경계 걸친 주 — frozen 전기 + 7일 연속
# ---------------------------------------------------------------------------


async def _freeze_prior_period(
    ctx: dict, *, start: date, end: date, frozen_days: list[DayDetail]
) -> UUID:
    """confirmed 전기 pay period + frozen entry (breakdown.days) 생성."""
    async with async_session() as db:
        period = PayPeriod(
            organization_id=ctx["org_id"],
            store_id=ctx["store_id"],
            start_date=start,
            end_date=end,
            status="confirmed",
            confirmed_at=datetime.now(timezone.utc),
        )
        db.add(period)
        await db.flush()
        breakdown = EntryBreakdown(calc_version=CALC_VERSION, days=frozen_days)
        entry = PayrollEntry(
            pay_period_id=period.id,
            organization_id=ctx["org_id"],
            store_id=ctx["store_id"],
            user_id=ctx["user_id"],
            org_member_id=ctx["member_id"],
            member_name="Calc Main",
            calc_version=CALC_VERSION,
            breakdown=breakdown.model_dump(mode="json"),
            regular_minutes=sum(d.regular_minutes for d in frozen_days),
        )
        db.add(entry)
        await db.commit()
        return period.id


async def test_straddle_week_frozen_prior_period(calc_ctx: dict) -> None:
    """전기가 confirmed — frozen breakdown.days 가 유일 원천 (계산 규칙 3).

    frozen 일~목 8h reg ×5 (40h straight) + live 금 8h + 토 8h.
    분류: 금 = 전부 weekly OT. 토 = 7일 연속 7일째 (8h 이내 → 전부 1.5x).
    손계산: (8h + 8h) × 1.5 × $20 = $480.00. regular $0.
    전기 일자에 남아 있는 live attendance(4h×5)는 **무시**되어야 한다 —
    live 를 읽으면 straight 20h 로 weekly OT 가 사라져 값이 달라진다.
    """
    frozen_days = [
        DayDetail(
            work_date=date(2026, 10, 11) + timedelta(days=i),
            regular_minutes=480,
            applied_rate=Decimal("20.00"),
        )
        for i in range(5)
    ]
    prior_id = await _freeze_prior_period(
        calc_ctx, start=date(2026, 10, 1), end=date(2026, 10, 15),
        frozen_days=frozen_days,
    )
    # 전기 일자의 상충 live attendance — frozen 이 원천이면 무시된다
    for i in range(5):
        await _mk_attendance(
            calc_ctx, work_date=date(2026, 10, 11) + timedelta(days=i),
            total_work_minutes=240,
        )
    await _mk_attendance(calc_ctx, work_date=date(2026, 10, 16), total_work_minutes=480)
    await _mk_attendance(calc_ctx, work_date=date(2026, 10, 17), total_work_minutes=480)

    rows = await _preview(calc_ctx, date(2026, 10, 20))
    row = _row_for(rows, calc_ctx["user_id"])

    assert [d.work_date for d in row.breakdown.days] == [
        date(2026, 10, 16), date(2026, 10, 17),
    ]
    # 동결 전기 일자도 근거로 남는다 — net 은 frozen 분류 합(8h), live 4h 가 아님
    assert [c.work_date for c in row.breakdown.context_days] == [
        date(2026, 10, 11) + timedelta(days=i) for i in range(5)
    ]
    assert {c.net_minutes for c in row.breakdown.context_days} == {480}
    assert (row.regular_minutes, row.ot_minutes, row.dt_minutes) == (0, 960, 0)
    assert row.regular_pay == Decimal("0.00")
    assert row.ot_pay == Decimal("480.00")

    weekly = await _events(calc_ctx, EVENT_KIND_WEEKLY_OT)
    assert [e.work_date for e in weekly] == [date(2026, 10, 16)]
    seventh = await _events(calc_ctx, EVENT_KIND_SEVENTH_DAY)
    assert [e.work_date for e in seventh] == [date(2026, 10, 17)]
    assert await _events(calc_ctx, EVENT_KIND_DAILY_OT) == []
    assert row.breakdown.sources == {
        "prior_period_ids": [str(prior_id)],
        "prior_period_frozen": True,
    }


# ---------------------------------------------------------------------------
# (f) 기간 중간 인상 — 가중평균 base (계산 규칙 1)
# ---------------------------------------------------------------------------


async def test_mid_period_raise_weighted_average_ot_base(calc_ctx: dict) -> None:
    """한 주에 rate 2개 ($20 → 수요일부터 $24) + 수요일 10h OT.

    손계산:
        분-가중평균 = (480×20 + 480×20 + 600×24) / 1560 = 33600/1560 = $21.538461...
        reg: 월+화 16h×$20 = $320.00, 수 8h×$24 = $192.00 → $512.00
        OT: 2h × 1.5 × 21.538461... = $64.6153... → $64.62 (센트 반올림 1회)
        gross = 512 + 64.62 + penalty
    """
    await _add_rate_history(calc_ctx, new_rate="20.00", effective=date(2026, 6, 1))
    await _add_rate_history(calc_ctx, new_rate="24.00", effective=_WED)
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=480)
    await _mk_attendance(calc_ctx, work_date=_TUE, total_work_minutes=480)
    await _mk_attendance(calc_ctx, work_date=_WED, total_work_minutes=600)

    rows = await _preview(calc_ctx, _JUL_MID)
    row = _row_for(rows, calc_ctx["user_id"])

    assert (row.regular_minutes, row.ot_minutes, row.dt_minutes) == (1440, 120, 0)
    assert row.regular_pay == Decimal("512.00")
    assert row.ot_pay == Decimal("64.62")

    # 구간: rate 별 2개 (명세서 rate별 표기 — R4)
    assert [s.rate for s in row.breakdown.segments] == [
        Decimal("20.00"), Decimal("24.00"),
    ]
    seg20, seg24 = row.breakdown.segments
    assert (seg20.regular_minutes, seg20.ot_minutes) == (960, 0)
    assert seg20.amount == Decimal("320.00")
    assert (seg24.regular_minutes, seg24.ot_minutes) == (480, 120)
    assert seg24.amount == Decimal("256.62")  # 192.00 + 64.62
    # 일별 적용 rate 확인
    by_date = {d.work_date: d for d in row.breakdown.days}
    assert by_date[_MON].applied_rate == Decimal("20.00")
    assert by_date[_WED].applied_rate == Decimal("24.00")


# ---------------------------------------------------------------------------
# (g) 7일 연속
# ---------------------------------------------------------------------------


async def test_seventh_consecutive_day(calc_ctx: dict) -> None:
    """2026-11-01(일)~11-07(토) 7일 연속 6h — 7일째 전부 1.5x.

    손계산: reg 36h×$20 = $720.00, OT 6h×1.5×$20 = $180.00.
    주는 기간(11/1~15) 안에 통째로 들어 sources 도 없다 (straddle 아님).
    """
    for i in range(7):
        await _mk_attendance(
            calc_ctx, work_date=date(2026, 11, 1) + timedelta(days=i),
            total_work_minutes=360,
        )

    rows = await _preview(calc_ctx, date(2026, 11, 10))
    row = _row_for(rows, calc_ctx["user_id"])

    assert (row.regular_minutes, row.ot_minutes, row.dt_minutes) == (2160, 360, 0)
    assert row.regular_pay == Decimal("720.00")
    assert row.ot_pay == Decimal("180.00")

    seventh = await _events(calc_ctx, EVENT_KIND_SEVENTH_DAY)
    assert [e.work_date for e in seventh] == [date(2026, 11, 7)]
    assert seventh[0].reason == "7th consecutive day worked in Sun-Sat workweek"
    assert await _events(calc_ctx, EVENT_KIND_WEEKLY_OT) == []
    assert await _events(calc_ctx, EVENT_KIND_DAILY_OT) == []
    assert row.breakdown.sources is None  # 11/1 이 일요일 — 경계 걸친 주 없음
    assert row.breakdown.context_days == []  # 주가 기간 안에 통째로 들어 근거 불필요


# ---------------------------------------------------------------------------
# (h) 같은 날 split shift 합산 → 일별 OT
# ---------------------------------------------------------------------------


async def test_split_shifts_sum_to_daily_ot(calc_ctx: dict) -> None:
    """같은 날 5h+5h split = 10h → 8h reg + 2h OT (일 grain 합산).

    손계산: (b) 와 동일 — reg $160.00 + OT $60.00.
    """
    sched1 = await _mk_schedule(calc_ctx, work_date=_MON)
    sched2 = await _mk_schedule(calc_ctx, work_date=_MON)
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=300, schedule_id=sched1)
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=300, schedule_id=sched2)

    rows = await _preview(calc_ctx, _JUL_MID)
    row = _row_for(rows, calc_ctx["user_id"])

    assert (row.regular_minutes, row.ot_minutes, row.dt_minutes) == (480, 120, 0)
    assert row.regular_pay == Decimal("160.00")
    assert row.ot_pay == Decimal("60.00")
    assert len(row.breakdown.days) == 1  # 일 단위 1행

    daily = await _events(calc_ctx, EVENT_KIND_DAILY_OT)
    assert len(daily) == 1
    assert daily[0].attendance_id is None  # 1:1 아님 → 참고 링크 없음


# ---------------------------------------------------------------------------
# (i) meal + rest penalty 금액
# ---------------------------------------------------------------------------


async def test_meal_and_rest_penalty_amounts(calc_ctx: dict) -> None:
    """6h10m(370분) 무휴게 → meal + rest 각 1h × 그날 rate.

    손계산: reg 370분×$20/60 = $123.333... → $123.33 (구간 반올림 1회).
    penalty = meal $20.00 + rest $20.00 = $40.00 → gross $163.33.
    """
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=370)

    rows = await _preview(calc_ctx, _JUL_MID)
    row = _row_for(rows, calc_ctx["user_id"])

    assert row.regular_pay == Decimal("123.33")
    assert row.penalty_pay == Decimal("40.00")
    assert row.gross_pay == Decimal("163.33")

    lines = row.breakdown.penalties
    assert [(l.kind, l.amount) for l in lines] == [
        (EVENT_KIND_MEAL_PENALTY, Decimal("20.00")),
        (EVENT_KIND_REST_PENALTY, Decimal("20.00")),
    ]
    assert lines[0].reason == "Worked 6.17h with 0 of 1 required 30-min meal break(s)"
    assert lines[1].reason == "Worked 6.17h with 0 of 2 required 10-min rest break(s)"
    assert all(l.work_date == _MON for l in lines)


# ---------------------------------------------------------------------------
# (j) validations
# ---------------------------------------------------------------------------


async def test_validations(calc_ctx: dict) -> None:
    """rate=0 / 최저임금 미달 / 열린 근무 / 자동퇴근 미확인 / 팁 미확정."""
    # 기본 user: rate 0 (org_members.hourly_rate=0 — rate_at 결과 0, 계산 규칙 5)
    async with async_session() as db:
        member = await db.get(OrgMember, calc_ctx["member_id"])
        member.hourly_rate = Decimal("0.00")
        await db.commit()
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=480)

    below_min = await _mk_user(calc_ctx, "Below Min", member_rate=Decimal("10.00"))
    await _mk_attendance(
        calc_ctx, user_id=below_min["user_id"], work_date=_MON, total_work_minutes=240
    )

    open_shift = await _mk_user(calc_ctx, "Open Shift")
    await _mk_attendance(
        calc_ctx, user_id=open_shift["user_id"], work_date=_TUE,
        total_work_minutes=None, status="working",
        clock_in=datetime(2026, 7, 7, 9, 0, tzinfo=timezone.utc),
    )

    auto_out = await _mk_user(calc_ctx, "Auto Out")
    await _mk_attendance(
        calc_ctx, user_id=auto_out["user_id"], work_date=_MON,
        total_work_minutes=480, anomalies=["auto_clocked_out"],
    )

    tipped = await _mk_user(calc_ctx, "Tipped Staff", member_rate=Decimal("20.00"))
    async with async_session() as db:
        tip_period = TipPeriod(
            store_id=calc_ctx["store_id"], start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 15), status="open",
        )
        db.add(tip_period)
        db.add(
            TipEntry(
                store_id=calc_ctx["store_id"], employee_id=tipped["user_id"],
                date=_MON, card_tips=Decimal("50.00"),
            )
        )
        await db.commit()
        await db.refresh(tip_period)
        tip_period_id = tip_period.id

    rows = await _preview(calc_ctx, _JUL_MID)

    # rate=0 — regular_pay 0, rate_missing (below_minimum 은 아님 — 0 은 미상 취급)
    zero_row = _row_for(rows, calc_ctx["user_id"])
    assert VALIDATION_RATE_MISSING in _codes(zero_row)
    assert VALIDATION_BELOW_MINIMUM_WAGE not in _codes(zero_row)
    assert zero_row.regular_pay == Decimal("0.00")
    assert zero_row.regular_minutes == 480  # 시간은 그대로 (rate 해소 시 재계산)
    assert "2026-07-06" in zero_row.validations[0].message

    # 최저임금 미달 — 계산은 그 rate 로 진행하되 경고
    bm_row = _row_for(rows, below_min["user_id"])
    assert VALIDATION_BELOW_MINIMUM_WAGE in _codes(bm_row)
    assert VALIDATION_RATE_MISSING not in _codes(bm_row)
    assert bm_row.regular_pay == Decimal("40.00")  # 4h × $10

    # 열린 근무 — 지급 시간 없음 + open_shift
    os_row = _row_for(rows, open_shift["user_id"])
    assert VALIDATION_OPEN_SHIFT in _codes(os_row)
    assert os_row.regular_minutes == 0
    assert os_row.gross_pay == Decimal("0.00")

    # 자동퇴근 미확인 — 지급은 정상 진행 + 경고
    ao_row = _row_for(rows, auto_out["user_id"])
    assert VALIDATION_UNCONFIRMED_AUTO_CLOCKOUT in _codes(ao_row)
    assert ao_row.regular_pay == Decimal("160.00")

    # 팁 미확정 — card_tips 는 포함하되 provisional 경고 (계산 규칙 4)
    tip_row = _row_for(rows, tipped["user_id"])
    assert VALIDATION_TIP_PERIOD_NOT_CONFIRMED in _codes(tip_row)
    assert tip_row.card_tips == Decimal("50.00")
    assert tip_row.breakdown.tip_period_id == str(tip_period_id)
    assert tip_row.gross_pay == tip_row.regular_pay + tip_row.penalty_pay + Decimal("50.00")

    # 팁 확정 후 재실행 — 경고 소멸 (다른 검증엔 영향 없음)
    async with async_session() as db:
        tp = await db.get(TipPeriod, tip_period_id)
        tp.status = "confirmed"
        await db.commit()
    rows2 = await _preview(calc_ctx, _JUL_MID)
    assert VALIDATION_TIP_PERIOD_NOT_CONFIRMED not in _codes(
        _row_for(rows2, tipped["user_id"])
    )


# ---------------------------------------------------------------------------
# 판정 보류 일의 잔존 penalty — rate 재조회 경로
# ---------------------------------------------------------------------------


async def test_held_day_penalty_uses_rate_lookup(calc_ctx: dict) -> None:
    """penalty 발생 후 근태 재오픈 — 일 net 미계산이라 지급 일에서 빠지지만
    잔존 penalty(판정 보류로 void 안 됨)는 rate_at 재조회로 금액을 매긴다."""
    att_id = await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=370)
    await _preview(calc_ctx, _JUL_MID)  # meal + rest 생성
    assert len(await _events(calc_ctx, EVENT_KIND_MEAL_PENALTY)) == 1

    # 재오픈 시뮬레이션 — clock_out 유실 (open shift)
    async with async_session() as db:
        att = await db.get(Attendance, att_id)
        att.total_work_minutes = None
        att.status = "working"
        att.clock_in = datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc)
        await db.commit()

    rows = await _preview(calc_ctx, _JUL_MID)
    row = _row_for(rows, calc_ctx["user_id"])

    assert row.regular_minutes == 0  # 지급 가능한 완결 일 없음
    assert row.penalty_pay == Decimal("40.00")  # 1h×$20 × 2건 (rate_at 재조회)
    assert row.gross_pay == Decimal("40.00")
    assert VALIDATION_OPEN_SHIFT in _codes(row)
    # penalty 이벤트는 보류로 유지 (void 아님)
    assert len(await _events(calc_ctx, EVENT_KIND_MEAL_PENALTY)) == 1


# ---------------------------------------------------------------------------
# (k) 일별 금액 — breakdown.days 의 표시용 regular/ot/dt/total
# ---------------------------------------------------------------------------


async def test_day_amounts_single_rate_week(calc_ctx: dict) -> None:
    """단일 rate 주 — 일별 금액 = 시간 × rate 정확.

    월 10h: reg 8h×$20 = $160.00, OT 2h×1.5×$20 = $60.00 → total $220.00.
    화 3h: reg $60.00. penalty($40.00)는 일별 금액에 들어가지 않는다
    (penalties 라인이 원천 — total_amount 는 근무 금액만).
    """
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=600)
    await _mk_attendance(calc_ctx, work_date=_TUE, total_work_minutes=180)

    rows = await _preview(calc_ctx, _JUL_MID)
    row = _row_for(rows, calc_ctx["user_id"])
    by_date = {d.work_date: d for d in row.breakdown.days}

    mon = by_date[_MON]
    assert (mon.regular_amount, mon.ot_amount, mon.dt_amount) == (
        Decimal("160.00"), Decimal("60.00"), Decimal("0.00"),
    )
    assert mon.total_amount == Decimal("220.00")
    tue = by_date[_TUE]
    assert tue.regular_amount == Decimal("60.00")  # 3h × $20
    assert tue.total_amount == Decimal("60.00")

    # 단일 rate + 나누어떨어지는 시간 — 일별 합이 구간 합(canonical)과 일치
    assert sum((d.total_amount for d in row.breakdown.days), Decimal("0")) == (
        row.regular_pay + row.ot_pay + row.dt_pay
    )
    assert row.penalty_pay == Decimal("40.00")  # 일별 금액엔 미포함


async def test_day_amounts_use_weighted_ot_base_on_raise_week(calc_ctx: dict) -> None:
    """인상 낀 주 — 정규는 그날 rate, OT 는 주 가중평균 base (계산 규칙 1).

    (f) 와 같은 시나리오: 월/화 8h@$20, 수 10h@$24.
        가중평균 = (480×20 + 480×20 + 600×24)/1560 = $21.538461...
        수: reg 8h×$24 = $192.00, OT 2h×1.5×21.538461… = $64.6153… → $64.62
        (그날 rate 로 계산했다면 $72.00 — 가중평균을 쓰는지 대조)
    """
    await _add_rate_history(calc_ctx, new_rate="20.00", effective=date(2026, 6, 1))
    await _add_rate_history(calc_ctx, new_rate="24.00", effective=_WED)
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=480)
    await _mk_attendance(calc_ctx, work_date=_TUE, total_work_minutes=480)
    await _mk_attendance(calc_ctx, work_date=_WED, total_work_minutes=600)

    rows = await _preview(calc_ctx, _JUL_MID)
    row = _row_for(rows, calc_ctx["user_id"])
    by_date = {d.work_date: d for d in row.breakdown.days}

    mon = by_date[_MON]
    assert mon.applied_rate == Decimal("20.00")
    assert (mon.regular_amount, mon.ot_amount) == (Decimal("160.00"), Decimal("0.00"))
    assert mon.total_amount == Decimal("160.00")

    wed = by_date[_WED]
    assert wed.applied_rate == Decimal("24.00")
    assert wed.regular_amount == Decimal("192.00")  # 정규는 그날 rate
    assert wed.ot_amount == Decimal("64.62")  # 가중평균 base × 1.5 × 2h
    assert wed.ot_amount != Decimal("72.00")  # 그날 rate 기준이 아님
    assert wed.total_amount == Decimal("256.62")

    # OT 일이 하루뿐이라 이 시나리오에선 일별 합 == 스칼라 (일반 보장은 아님)
    assert sum((d.total_amount for d in row.breakdown.days), Decimal("0")) == (
        row.regular_pay + row.ot_pay
    )


async def test_day_amounts_include_double_time(calc_ctx: dict) -> None:
    """13h 하루 — reg $160.00 + OT $120.00 + DT $40.00 → total $320.00 ((c) 손계산)."""
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=780)

    rows = await _preview(calc_ctx, _JUL_MID)
    row = _row_for(rows, calc_ctx["user_id"])

    assert len(row.breakdown.days) == 1
    day = row.breakdown.days[0]
    assert (day.regular_amount, day.ot_amount, day.dt_amount) == (
        Decimal("160.00"), Decimal("120.00"), Decimal("40.00"),
    )
    assert day.total_amount == Decimal("320.00")


async def test_day_amounts_zero_when_rate_missing(calc_ctx: dict) -> None:
    """rate 미상 일 — 금액은 None 이 아니라 $0.00 (경고는 validations 담당)."""
    async with async_session() as db:
        member = await db.get(OrgMember, calc_ctx["member_id"])
        member.hourly_rate = Decimal("0.00")
        await db.commit()
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=600)

    rows = await _preview(calc_ctx, _JUL_MID)
    row = _row_for(rows, calc_ctx["user_id"])

    day = row.breakdown.days[0]
    assert (day.regular_amount, day.ot_amount, day.dt_amount) == (
        Decimal("0.00"), Decimal("0.00"), Decimal("0.00"),
    )
    assert day.total_amount == Decimal("0.00")
    assert day.regular_minutes == 480  # 시간은 그대로 (rate 해소 시 재계산)
    assert VALIDATION_RATE_MISSING in _codes(row)


# ---------------------------------------------------------------------------
# (l) 일별 근무/휴게 시각 (store-tz 벽시계)
# ---------------------------------------------------------------------------


async def test_day_shifts_and_breaks_use_store_wall_clock(calc_ctx: dict) -> None:
    """clock_in/out·휴게가 매장 타임존 "HH:MM" 로 실린다 (표시 전용).

    store tz 는 UTC 픽스처 — 09:00~15:30 근무 + 무급 식사 30m + 유급 10m.
    """
    att_id = await _mk_attendance(
        calc_ctx, work_date=_MON, total_work_minutes=360,
        clock_in=datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc),
        clock_out=datetime(2026, 7, 6, 15, 30, tzinfo=timezone.utc),
    )
    async with async_session() as db:
        db.add_all([
            AttendanceBreak(
                attendance_id=att_id, break_type="paid_10min",
                started_at=datetime(2026, 7, 6, 11, 0, tzinfo=timezone.utc),
                ended_at=datetime(2026, 7, 6, 11, 10, tzinfo=timezone.utc),
                duration_minutes=10,
            ),
            AttendanceBreak(
                attendance_id=att_id, break_type="unpaid_meal",
                started_at=datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
                ended_at=datetime(2026, 7, 6, 12, 30, tzinfo=timezone.utc),
                duration_minutes=30,
            ),
        ])
        await db.commit()

    rows = await _preview(calc_ctx, _JUL_MID)
    day = _row_for(rows, calc_ctx["user_id"]).breakdown.days[0]

    assert [(s.start, s.end) for s in day.shifts] == [("09:00", "15:30")]
    assert [(b.start, b.end, b.type) for b in day.breaks] == [
        ("11:00", "11:10", "paid_10min"),
        ("12:00", "12:30", "unpaid_meal"),
    ]


async def test_day_shifts_list_each_split_shift(calc_ctx: dict) -> None:
    """분할 근무 — 구간이 시간순으로 각각 남는다 (분 합산과 별개)."""
    sched1 = await _mk_schedule(calc_ctx, work_date=_MON)
    sched2 = await _mk_schedule(calc_ctx, work_date=_MON)
    await _mk_attendance(
        calc_ctx, work_date=_MON, total_work_minutes=240, schedule_id=sched1,
        clock_in=datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc),
        clock_out=datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
    )
    await _mk_attendance(
        calc_ctx, work_date=_MON, total_work_minutes=240, schedule_id=sched2,
        clock_in=datetime(2026, 7, 6, 17, 0, tzinfo=timezone.utc),
        clock_out=datetime(2026, 7, 6, 21, 0, tzinfo=timezone.utc),
    )

    rows = await _preview(calc_ctx, _JUL_MID)
    day = _row_for(rows, calc_ctx["user_id"]).breakdown.days[0]

    assert [(s.start, s.end) for s in day.shifts] == [
        ("09:00", "13:00"), ("17:00", "21:00"),
    ]


async def test_day_without_clock_times_has_empty_windows(calc_ctx: dict) -> None:
    """clock 기록 없이 분만 있는 근태(수기 입력) — 시각 목록은 빈 채로 둔다."""
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=480)

    rows = await _preview(calc_ctx, _JUL_MID)
    day = _row_for(rows, calc_ctx["user_id"]).breakdown.days[0]

    assert day.shifts == []
    assert day.breaks == []
    assert day.regular_minutes == 480  # 지급은 그대로 (분이 원천)


# ---------------------------------------------------------------------------
# 멱등성 / 결정성
# ---------------------------------------------------------------------------


async def test_preview_is_deterministic_and_idempotent(calc_ctx: dict) -> None:
    """같은 입력에 두 번 실행 → 동일 행 + 이벤트 중복 생성 없음."""
    for i in range(6):
        await _mk_attendance(calc_ctx, work_date=_SUN + timedelta(days=i), total_work_minutes=420)

    rows1 = await _preview(calc_ctx, _JUL_MID)
    events1 = await _events(calc_ctx)
    rows2 = await _preview(calc_ctx, _JUL_MID)
    events2 = await _events(calc_ctx)

    assert rows1 == rows2  # pydantic 모델 동등성 — 전체 필드 일치
    assert len(events1) == len(events2)
    assert {e.id for e in events1} == {e.id for e in events2}


# ---------------------------------------------------------------------------
# 로스터 게이트 — 빈 행 제외 / 계정 상태 무관 (has_payroll_activity)
# ---------------------------------------------------------------------------


async def test_zero_net_completed_attendance_is_not_a_row(calc_ctx: dict) -> None:
    """완결됐는데 net 0분 — 지급할 것도 경고도 없으니 목록에 없다."""
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=0)

    rows = await _preview(calc_ctx, _JUL_MID)

    assert [r for r in rows if r.user_id == calc_ctx["user_id"]] == []


async def test_deactivated_account_with_hours_stays_in_roster(calc_ctx: dict) -> None:
    """비활성 계정이라도 기간에 근무가 있으면 지급 대상 — 숨기지 않는다."""
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=480)
    async with async_session() as db:
        user = await db.get(User, calc_ctx["user_id"])
        user.is_active = False
        user.status = "deactivated"
        await db.commit()

    rows = await _preview(calc_ctx, _JUL_MID)

    assert _row_for(rows, calc_ctx["user_id"]).regular_minutes == 480


async def test_open_shift_with_no_minutes_stays_as_warning_row(calc_ctx: dict) -> None:
    """미퇴근 — 분은 0 이지만 해결할 경고가 있으면 남는다 (숨기면 급여 누락)."""
    await _mk_attendance(
        calc_ctx, work_date=_TUE, total_work_minutes=None, status="working",
        clock_in=datetime(2026, 7, 7, 9, 0, tzinfo=timezone.utc),
    )

    rows = await _preview(calc_ctx, _JUL_MID)
    row = _row_for(rows, calc_ctx["user_id"])

    assert row.regular_minutes == 0
    assert VALIDATION_OPEN_SHIFT in _codes(row)


async def test_no_show_attendance_stays_as_warning_row(calc_ctx: dict) -> None:
    """cron 이 no_show 로 승격한 근무 — 결근인지 기록 누락인지 확정 전 사람이 본다."""
    sched = await _mk_schedule(calc_ctx, work_date=_TUE)
    await _mk_attendance(
        calc_ctx, work_date=_TUE, total_work_minutes=None, status="no_show",
        schedule_id=sched,
    )

    rows = await _preview(calc_ctx, _JUL_MID)
    row = _row_for(rows, calc_ctx["user_id"])

    assert row.regular_minutes == 0
    assert VALIDATION_NO_SHOW in _codes(row)
    assert VALIDATION_OPEN_SHIFT not in _codes(row)  # clock_in 자체가 없다
    no_show = next(v for v in row.validations if v.code == VALIDATION_NO_SHOW)
    assert str(_TUE) in no_show.message


async def test_upcoming_attendance_is_not_a_row(calc_ctx: dict) -> None:
    """upcoming(판정 전) 은 경고가 아니다 — open 기간의 미래 근무가 뜨면 안 된다."""
    sched = await _mk_schedule(calc_ctx, work_date=_TUE)
    await _mk_attendance(
        calc_ctx, work_date=_TUE, total_work_minutes=None, status="upcoming",
        schedule_id=sched,
    )

    rows = await _preview(calc_ctx, _JUL_MID)

    assert [r for r in rows if r.user_id == calc_ctx["user_id"]] == []


# ---------------------------------------------------------------------------
# 일자별 매장/attendance — 콘솔 근태 딥링크의 원천 (group 스코프)
# ---------------------------------------------------------------------------


async def test_day_detail_carries_store_and_attendance_for_deeplink(
    calc_ctx: dict
) -> None:
    """group 기간은 period 에 매장이 없다 — 일자별 매장이 breakdown 에 있어야
    콘솔이 "이 날 근태" 로 정확히 이동한다. attendance 가 1건이면 상세로 직행."""
    att_id = await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=480)

    rows = await _preview(calc_ctx, _JUL_MID)
    day = next(
        d for d in _row_for(rows, calc_ctx["user_id"]).breakdown.days
        if d.work_date == _MON
    )

    assert day.store_id == str(calc_ctx["store_id"])
    assert day.store_ids == [str(calc_ctx["store_id"])]
    assert day.attendance_id == str(att_id)  # 1:1 → 상세 페이지로 바로 열 수 있다


async def test_day_detail_attendance_is_none_for_split_shift(calc_ctx: dict) -> None:
    """같은 날 근무가 둘이면 하나로 특정할 수 없다 — 목록 필터로 폴백."""
    s1 = await _mk_schedule(calc_ctx, work_date=_MON)
    s2 = await _mk_schedule(calc_ctx, work_date=_MON)
    await _mk_attendance(
        calc_ctx, work_date=_MON, total_work_minutes=240, schedule_id=s1
    )
    await _mk_attendance(
        calc_ctx, work_date=_MON, total_work_minutes=180, schedule_id=s2
    )

    rows = await _preview(calc_ctx, _JUL_MID)
    day = next(
        d for d in _row_for(rows, calc_ctx["user_id"]).breakdown.days
        if d.work_date == _MON
    )

    assert day.attendance_id is None
    assert day.store_id == str(calc_ctx["store_id"])  # 매장은 여전히 특정된다
