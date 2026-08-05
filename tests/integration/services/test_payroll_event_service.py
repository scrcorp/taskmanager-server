"""Integration — payroll_event_service (Payroll v1 Phase 2).

대상: app/services/payroll_event_service.py
    - detect_and_upsert_events: meal/rest penalty 재감지 + 일 키 upsert
      (생성 / reason 갱신 / attribution 보존 / void / revive / frozen 불변 /
       cancelled 제외 / 미퇴근 일 판정 보류 / split shift 합산)
    - upsert_classification_events: 계산 엔진 분류 출력 반영 (동일 의미론)
    - list_events / tag_event

스펙: docs/99_inbox/2026-08-03 payroll-v1-스키마-스펙.md §6, 설계방향 C5/E1.
서비스는 commit 하지 않으므로(호출자 트랜잭션 소유) 테스트가 직접 commit 한다.
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import date, datetime, timedelta, timezone
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
from app.models.attendance_break import (
    BREAK_TYPE_PAID_10MIN,
    BREAK_TYPE_PAID_SHORT,
    BREAK_TYPE_UNPAID_LONG,
    BREAK_TYPE_UNPAID_MEAL,
    AttendanceBreak,
)
from app.models.organization import Store
from app.models.payroll import PayPeriod, PayrollEvent
from app.models.schedule import Schedule
from app.models.user import User
from app.services.payroll_event_service import (
    ClassifiedDay,
    payroll_event_service,
)
from app.utils.exceptions import BadRequestError, NotFoundError

_D1 = date(2026, 7, 6)  # 기준 근무일 (고정 — store tz 무관, work_date 직접 세팅)
_D2 = date(2026, 7, 7)
_D3 = date(2026, 7, 8)


# ---------------------------------------------------------------------------
# 픽스처 — throwaway store + user (function scope, 종료 시 전부 정리)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def event_ctx(
    seed_organization: dict, seed_roles: dict[str, UUID]
) -> AsyncIterator[dict]:
    """전용 store/user. 정리 순서: events → periods → attendances → schedules → store/user."""
    org_id: UUID = seed_organization["id"]
    suffix = uuid_mod.uuid4().hex[:8]
    async with async_session() as db:
        store = Store(
            organization_id=org_id,
            name=f"__payroll_event_store_{suffix}",
            timezone="UTC",
            day_start_time={"all": "00:00"},
        )
        user = User(
            organization_id=org_id,
            role_id=seed_roles["staff"],
            username=f"__payroll_event_user_{suffix}",
            full_name="Payroll Event Test",
            password_hash="x",
            is_active=True,
        )
        db.add_all([store, user])
        await db.commit()
        await db.refresh(store)
        await db.refresh(user)
        ctx = {"org_id": org_id, "store_id": store.id, "user_id": user.id}

    yield ctx

    async with async_session() as db:
        await db.execute(
            delete(PayrollEvent).where(PayrollEvent.store_id == ctx["store_id"])
        )
        await db.execute(
            delete(PayPeriod).where(PayPeriod.store_id == ctx["store_id"])
        )
        # attendance_breaks 는 FK CASCADE 로 같이 삭제
        await db.execute(
            delete(Attendance).where(Attendance.store_id == ctx["store_id"])
        )
        await db.execute(
            delete(Schedule).where(Schedule.store_id == ctx["store_id"])
        )
        await db.execute(delete(Store).where(Store.id == ctx["store_id"]))
        await db.execute(delete(User).where(User.id == ctx["user_id"]))
        await db.commit()


# ---------------------------------------------------------------------------
# 헬퍼 — attendance / break / schedule 생성 + detect 실행
# ---------------------------------------------------------------------------


async def _mk_attendance(
    ctx: dict,
    *,
    work_date: date = _D1,
    total_work_minutes: int | None,
    status: str = "clocked_out",
    schedule_id: UUID | None = None,
) -> UUID:
    """clocked_out attendance (net = total - break 차감은 C1 공식이 계산)."""
    async with async_session() as db:
        att = Attendance(
            organization_id=ctx["org_id"],
            store_id=ctx["store_id"],
            user_id=ctx["user_id"],
            schedule_id=schedule_id,
            work_date=work_date,
            status=status,
            total_work_minutes=total_work_minutes,
        )
        db.add(att)
        await db.commit()
        await db.refresh(att)
        return att.id


async def _mk_schedule(ctx: dict, *, work_date: date = _D1) -> UUID:
    """split shift 용 스케줄 (walk-in unique 회피 — schedule 단위 attendance 허용)."""
    async with async_session() as db:
        sched = Schedule(
            organization_id=ctx["org_id"],
            user_id=ctx["user_id"],
            store_id=ctx["store_id"],
            operating_day=work_date,
            start_at=datetime.combine(work_date, datetime.min.time()),
            end_at=datetime.combine(work_date, datetime.min.time())
            + timedelta(hours=4),
            status="confirmed",
        )
        db.add(sched)
        await db.commit()
        await db.refresh(sched)
        return sched.id


async def _mk_break(
    attendance_id: UUID,
    break_type: str,
    minutes: int,
) -> UUID:
    async with async_session() as db:
        base = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
        br = AttendanceBreak(
            attendance_id=attendance_id,
            started_at=base,
            ended_at=base + timedelta(minutes=minutes),
            break_type=break_type,
            duration_minutes=minutes,
        )
        db.add(br)
        await db.commit()
        await db.refresh(br)
        return br.id


async def _detect(ctx: dict, start: date = _D1, end: date = _D3):
    """서비스 호출 + commit (호출자 트랜잭션 소유 관례)."""
    async with async_session() as db:
        summary = await payroll_event_service.detect_and_upsert_events(
            db, ctx["store_id"], start, end
        )
        await db.commit()
        return summary


async def _events(ctx: dict, kind: str | None = None) -> list[PayrollEvent]:
    async with async_session() as db:
        query = select(PayrollEvent).where(
            PayrollEvent.store_id == ctx["store_id"]
        )
        if kind is not None:
            query = query.where(PayrollEvent.kind == kind)
        return list((await db.execute(query.order_by(PayrollEvent.work_date))).scalars().all())


# ---------------------------------------------------------------------------
# detect — 생성 분기
# ---------------------------------------------------------------------------


async def test_detect_creates_meal_and_rest_penalties(event_ctx: dict) -> None:
    """6h10m 무휴게 근무 → meal + rest 각 1건 (attendance 1:1 링크 포함)."""
    att_id = await _mk_attendance(event_ctx, total_work_minutes=370)

    summary = await _detect(event_ctx)
    assert summary.created == 2
    assert summary.voided == 0

    events = await _events(event_ctx)
    by_kind = {e.kind: e for e in events}
    assert set(by_kind) == {EVENT_KIND_MEAL_PENALTY, EVENT_KIND_REST_PENALTY}

    meal = by_kind[EVENT_KIND_MEAL_PENALTY]
    assert meal.reason == "Worked 6.2h with no 30-min meal break"
    assert meal.work_date == _D1
    assert meal.user_id == event_ctx["user_id"]
    assert meal.attendance_id == att_id  # 그날 attendance 1건 → 1:1 링크
    assert meal.voided_at is None
    assert meal.attribution is None  # 미태깅

    rest = by_kind[EVENT_KIND_REST_PENALTY]
    assert rest.reason == "Worked 6.2h with 0 of 2 required 10-min rest break(s)"


async def test_detect_no_penalty_when_breaks_satisfied(event_ctx: dict) -> None:
    """5h 근무 + meal 30분 + 유급 10분 → net 4.5h, 위반 없음."""
    att_id = await _mk_attendance(event_ctx, total_work_minutes=300)
    await _mk_break(att_id, BREAK_TYPE_UNPAID_MEAL, 30)
    await _mk_break(att_id, BREAK_TYPE_PAID_10MIN, 10)

    summary = await _detect(event_ctx)
    assert summary.created == 0
    assert await _events(event_ctx) == []


async def test_detect_legacy_break_types_recognized(event_ctx: dict) -> None:
    """레거시 unpaid_long/paid_short 세션으로도 충족 판정 (dual-read)."""
    att_id = await _mk_attendance(event_ctx, total_work_minutes=380)
    await _mk_break(att_id, BREAK_TYPE_UNPAID_LONG, 40)  # meal 충족
    await _mk_break(att_id, BREAK_TYPE_PAID_SHORT, 10)  # rest 충족 (net 340 → 1개 필요)

    summary = await _detect(event_ctx)
    assert summary.created == 0
    assert await _events(event_ctx) == []


async def test_detect_split_shift_sums_to_one_penalty_per_kind(event_ctx: dict) -> None:
    """같은 날 3h+3h split shift → 일 합산 6h, kind 당 1건. attendance_id 는 NULL."""
    sched1 = await _mk_schedule(event_ctx, work_date=_D1)
    sched2 = await _mk_schedule(event_ctx, work_date=_D1)
    await _mk_attendance(event_ctx, total_work_minutes=180, schedule_id=sched1)
    await _mk_attendance(event_ctx, total_work_minutes=180, schedule_id=sched2)

    summary = await _detect(event_ctx)
    assert summary.created == 2  # meal(360>300) + rest(360분 → 1개 필요, 0개)

    meal_events = await _events(event_ctx, EVENT_KIND_MEAL_PENALTY)
    assert len(meal_events) == 1  # split 이중부과 없음 (일 grain)
    assert meal_events[0].attendance_id is None  # 1:1 아님 → 참고 링크 없음
    assert "6.0h" in meal_events[0].reason


async def test_detect_cancelled_attendance_excluded(event_ctx: dict) -> None:
    """cancelled attendance 는 입력 제외 — 이벤트 생성 없음."""
    await _mk_attendance(event_ctx, total_work_minutes=370, status="cancelled")

    summary = await _detect(event_ctx)
    assert summary.created == 0
    assert await _events(event_ctx) == []


async def test_detect_open_day_held_not_voided(event_ctx: dict) -> None:
    """그날 전부 미퇴근(net 미계산)이면 판정 보류 — 기존 이벤트 유지."""
    att_id = await _mk_attendance(event_ctx, total_work_minutes=370)
    await _detect(event_ctx)
    assert len(await _events(event_ctx)) == 2

    # 재오픈 시뮬레이션 — clock_out 이 사라진 상태 (total_work_minutes NULL)
    async with async_session() as db:
        att = await db.get(Attendance, att_id)
        att.total_work_minutes = None
        att.status = "working"
        await db.commit()

    summary = await _detect(event_ctx)
    assert summary.voided == 0  # 보류 — void 하지 않음
    for event in await _events(event_ctx):
        assert event.voided_at is None


# ---------------------------------------------------------------------------
# detect — upsert / void / revive / attribution 보존
# ---------------------------------------------------------------------------


async def test_upsert_updates_reason_preserves_attribution(event_ctx: dict) -> None:
    """재감지 시 reason 은 갱신, attribution/tagged_by/tagged_at 은 보존."""
    att_id = await _mk_attendance(event_ctx, total_work_minutes=370)
    await _detect(event_ctx)

    meal = (await _events(event_ctx, EVENT_KIND_MEAL_PENALTY))[0]
    async with async_session() as db:
        tagged = await payroll_event_service.tag_event(
            db, meal.id, "management", event_ctx["user_id"]
        )
        original_tagged_at = tagged.tagged_at
    assert tagged.attribution == "management"

    # 근무시간 정정 (7h) → 재감지
    async with async_session() as db:
        att = await db.get(Attendance, att_id)
        att.total_work_minutes = 420
        await db.commit()

    summary = await _detect(event_ctx)
    assert summary.updated >= 1
    assert summary.created == 0

    meal = (await _events(event_ctx, EVENT_KIND_MEAL_PENALTY))[0]
    assert meal.reason == "Worked 7.0h with no 30-min meal break"  # 갱신
    assert meal.attribution == "management"  # 보존
    assert meal.tagged_by == event_ctx["user_id"]
    assert meal.tagged_at == original_tagged_at


async def test_void_then_revive_preserves_tagging(event_ctx: dict) -> None:
    """조건 소멸 → voided_at 마킹(행 보존), 조건 재발 → revive. 태그는 내내 보존."""
    att_id = await _mk_attendance(event_ctx, total_work_minutes=370)
    await _detect(event_ctx)
    meal = (await _events(event_ctx, EVENT_KIND_MEAL_PENALTY))[0]
    async with async_session() as db:
        await payroll_event_service.tag_event(
            db, meal.id, "staff", event_ctx["user_id"]
        )

    # meal break 추가 → meal 조건 소멸
    br_id = await _mk_break(att_id, BREAK_TYPE_UNPAID_MEAL, 30)
    summary = await _detect(event_ctx)
    assert summary.voided >= 1

    meal = (await _events(event_ctx, EVENT_KIND_MEAL_PENALTY))[0]
    assert meal.voided_at is not None  # 삭제 아닌 마킹
    assert meal.attribution == "staff"  # 태깅 보존

    # break 삭제 → 조건 재발 → revive
    async with async_session() as db:
        await db.execute(
            delete(AttendanceBreak).where(AttendanceBreak.id == br_id)
        )
        await db.commit()

    summary = await _detect(event_ctx)
    assert summary.revived == 1
    meal = (await _events(event_ctx, EVENT_KIND_MEAL_PENALTY))[0]
    assert meal.voided_at is None
    assert meal.attribution == "staff"  # revive 후에도 보존


async def test_frozen_rows_untouched(event_ctx: dict) -> None:
    """pay_period_id 부여(동결) 행은 재감지가 갱신도 void 도 하지 않는다."""
    att_id = await _mk_attendance(event_ctx, total_work_minutes=370)
    await _detect(event_ctx)

    async with async_session() as db:
        period = PayPeriod(
            organization_id=event_ctx["org_id"],
            store_id=event_ctx["store_id"],
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 15),
        )
        db.add(period)
        await db.flush()
        for event in (
            await db.execute(
                select(PayrollEvent).where(
                    PayrollEvent.store_id == event_ctx["store_id"]
                )
            )
        ).scalars():
            event.pay_period_id = period.id
        await db.commit()

    # 조건 유지 상태에서 근무시간 변경 → frozen 이라 reason 불변
    async with async_session() as db:
        att = await db.get(Attendance, att_id)
        att.total_work_minutes = 420
        await db.commit()
    summary = await _detect(event_ctx)
    assert summary.skipped_frozen >= 1
    assert summary.updated == 0
    meal = (await _events(event_ctx, EVENT_KIND_MEAL_PENALTY))[0]
    assert "6.2h" in meal.reason  # 갱신 안 됨

    # 조건 소멸시켜도 frozen 은 void 안 됨
    await _mk_break(att_id, BREAK_TYPE_UNPAID_MEAL, 30)
    summary = await _detect(event_ctx)
    assert summary.voided == 0
    for event in await _events(event_ctx):
        assert event.voided_at is None


async def test_void_on_attendance_cancelled(event_ctx: dict) -> None:
    """근태가 cancelled 되면 기존 이벤트는 void (조건 소멸 취급)."""
    att_id = await _mk_attendance(event_ctx, total_work_minutes=370)
    await _detect(event_ctx)
    assert len(await _events(event_ctx)) == 2

    async with async_session() as db:
        att = await db.get(Attendance, att_id)
        att.status = "cancelled"
        await db.commit()

    summary = await _detect(event_ctx)
    assert summary.voided == 2
    for event in await _events(event_ctx):
        assert event.voided_at is not None  # 행은 보존


# ---------------------------------------------------------------------------
# upsert_classification_events — 계산 엔진 분류 반영
# ---------------------------------------------------------------------------


async def _classify(ctx: dict, days: list[ClassifiedDay], start: date = _D1, end: date = _D3):
    async with async_session() as db:
        summary = await payroll_event_service.upsert_classification_events(
            db, ctx["store_id"], start, end, days
        )
        await db.commit()
        return summary


async def test_classification_events_created(event_ctx: dict) -> None:
    """daily_ot(+DT 합산 표기)/weekly_ot/seventh_day 각 kind 생성 + reason 포맷."""
    att_id = await _mk_attendance(event_ctx, total_work_minutes=780)
    days = [
        ClassifiedDay(
            user_id=event_ctx["user_id"], work_date=_D1,
            daily_ot_minutes=240, daily_dt_minutes=60, attendance_id=att_id,
        ),
        ClassifiedDay(
            user_id=event_ctx["user_id"], work_date=_D2, weekly_ot_minutes=90,
        ),
        ClassifiedDay(
            user_id=event_ctx["user_id"], work_date=_D3, seventh_day=True,
        ),
    ]
    summary = await _classify(event_ctx, days)
    assert summary.created == 3

    events = {e.kind: e for e in await _events(event_ctx)}
    assert set(events) == {
        EVENT_KIND_DAILY_OT, EVENT_KIND_WEEKLY_OT, EVENT_KIND_SEVENTH_DAY,
    }
    assert events[EVENT_KIND_DAILY_OT].reason == (
        "Daily overtime: 4.0h over 8h/day at 1.5x, 1.0h over 12h/day at 2x"
    )
    assert events[EVENT_KIND_DAILY_OT].attendance_id == att_id
    assert events[EVENT_KIND_WEEKLY_OT].reason == (
        "Weekly overtime: 1.5h over 40h/week at 1.5x"
    )
    assert events[EVENT_KIND_WEEKLY_OT].attendance_id is None
    assert events[EVENT_KIND_SEVENTH_DAY].reason == (
        "7th consecutive day worked in Sun-Sat workweek"
    )


async def test_classification_authoritative_voids_missing_days(event_ctx: dict) -> None:
    """다음 실행 목록에서 빠진 분류는 void, 유지된 것은 reason 갱신."""
    days = [
        ClassifiedDay(user_id=event_ctx["user_id"], work_date=_D1, daily_ot_minutes=60),
        ClassifiedDay(user_id=event_ctx["user_id"], work_date=_D2, weekly_ot_minutes=90),
    ]
    await _classify(event_ctx, days)

    # 재계산 결과: D1 OT 만 남고 분량 변경, D2 weekly 소멸
    days2 = [
        ClassifiedDay(user_id=event_ctx["user_id"], work_date=_D1, daily_ot_minutes=120),
    ]
    summary = await _classify(event_ctx, days2)
    assert summary.updated == 1
    assert summary.voided == 1

    daily = (await _events(event_ctx, EVENT_KIND_DAILY_OT))[0]
    assert daily.voided_at is None
    assert "2.0h over 8h/day" in daily.reason
    weekly = (await _events(event_ctx, EVENT_KIND_WEEKLY_OT))[0]
    assert weekly.voided_at is not None


async def test_classification_ignores_out_of_range_days(event_ctx: dict) -> None:
    """범위 밖 work_date 항목은 무시 (비대칭 잔행 방지)."""
    days = [
        ClassifiedDay(
            user_id=event_ctx["user_id"],
            work_date=_D3 + timedelta(days=30),
            daily_ot_minutes=60,
        ),
    ]
    summary = await _classify(event_ctx, days)
    assert summary.created == 0
    assert await _events(event_ctx) == []


async def test_classification_does_not_touch_penalty_kinds(event_ctx: dict) -> None:
    """분류 upsert 는 penalty kind 를 소유하지 않는다 — void 스캔 분리."""
    await _mk_attendance(event_ctx, total_work_minutes=370)
    await _detect(event_ctx)  # meal + rest 생성

    summary = await _classify(event_ctx, [])  # 분류 없음
    assert summary.voided == 0  # penalty 행은 안 건드림
    for event in await _events(event_ctx):
        assert event.voided_at is None


# ---------------------------------------------------------------------------
# list_events / tag_event
# ---------------------------------------------------------------------------


async def test_list_events_filters_voided_and_range(event_ctx: dict) -> None:
    """기본은 유효 행만, include_voided=True 면 전부. 기간 필터 동작."""
    att_id = await _mk_attendance(event_ctx, total_work_minutes=370)
    await _detect(event_ctx)
    await _mk_break(att_id, BREAK_TYPE_UNPAID_MEAL, 30)  # meal 조건 소멸
    await _detect(event_ctx)  # meal void, rest 는 유지 (net 340 → 1개 필요)

    async with async_session() as db:
        active = await payroll_event_service.list_events(
            db, event_ctx["store_id"], _D1, _D3
        )
        assert [e.kind for e in active] == [EVENT_KIND_REST_PENALTY]

        everything = await payroll_event_service.list_events(
            db, event_ctx["store_id"], _D1, _D3, include_voided=True
        )
        assert len(everything) == 2

        out_of_range = await payroll_event_service.list_events(
            db, event_ctx["store_id"], _D1 + timedelta(days=90), _D1 + timedelta(days=99)
        )
        assert out_of_range == []


async def test_tag_event_validation(event_ctx: dict) -> None:
    """attribution 화이트리스트 / 미존재 / frozen 거부 + 정상 태깅."""
    await _mk_attendance(event_ctx, total_work_minutes=370)
    await _detect(event_ctx)
    meal = (await _events(event_ctx, EVENT_KIND_MEAL_PENALTY))[0]

    # 잘못된 attribution → 400
    async with async_session() as db:
        try:
            await payroll_event_service.tag_event(
                db, meal.id, "himself", event_ctx["user_id"]
            )
            raise AssertionError("invalid attribution must be rejected")
        except BadRequestError as exc:
            assert "Invalid attribution" in exc.detail

    # 미존재 이벤트 → 404
    async with async_session() as db:
        try:
            await payroll_event_service.tag_event(
                db, uuid_mod.uuid4(), "staff", event_ctx["user_id"]
            )
            raise AssertionError("unknown event must raise NotFoundError")
        except NotFoundError:
            pass

    # 정상 태깅 — attribution/tagged_by/tagged_at 기록
    async with async_session() as db:
        tagged = await payroll_event_service.tag_event(
            db, meal.id, "staff", event_ctx["user_id"]
        )
        assert tagged.attribution == "staff"
        assert tagged.tagged_by == event_ctx["user_id"]
        assert tagged.tagged_at is not None

    # frozen → 400 (amendment 전용)
    async with async_session() as db:
        period = PayPeriod(
            organization_id=event_ctx["org_id"],
            store_id=event_ctx["store_id"],
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 15),
        )
        db.add(period)
        await db.flush()
        event = await db.get(PayrollEvent, meal.id)
        event.pay_period_id = period.id
        await db.commit()

    async with async_session() as db:
        try:
            await payroll_event_service.tag_event(
                db, meal.id, "management", event_ctx["user_id"]
            )
            raise AssertionError("frozen event must reject tagging")
        except BadRequestError as exc:
            assert "confirmed pay period" in exc.detail
