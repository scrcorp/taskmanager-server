"""payroll 3테이블 마이그레이션 스모크 테스트 — Payroll v1 Phase 2.

검증 대상 (스펙: docs/99_inbox/2026-08-03 payroll-v1-스키마-스펙.md §4/§5/§6,
마이그레이션 c659422e16ae):
    - 테이블/인덱스/제약 존재 + FK delete rule (pay_periods.store RESTRICT 등)
    - pay_periods: status 기본 'open', uq_pay_period_store_start 중복 거부,
      store 삭제는 기간 존재 시 차단(RESTRICT)
    - payroll_entries: revision DB default 0, breakdown NOT NULL,
      uq_payroll_entry_period_user_rev partial unique (user NULL 은 검사 제외),
      pay_period 삭제 시 CASCADE
    - payroll_events: 일 단위 unique (하루 1건/kind), user NULL 예외,
      voided_at 마킹, pay_period 삭제 시 SET NULL (이벤트 보존)
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import AsyncIterator
from uuid import UUID

import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.models.organization import Store
from app.models.payroll import PayPeriod, PayrollEntry, PayrollEvent
from app.models.user import User


# ---------------------------------------------------------------------------
# 픽스처 — 테스트 전용 throwaway store + user (RESTRICT 테스트가 store 삭제를
# 시도하므로 공용 시드 store 를 쓰면 안 됨)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def payroll_env(seed_organization: dict, seed_roles: dict[str, UUID]) -> AsyncIterator[dict]:
    """throwaway store + user. 종료 시 events→entries→periods→store→user 순 정리."""
    org_id: UUID = seed_organization["id"]
    suffix = uuid_mod.uuid4().hex[:8]
    async with async_session() as db:
        store = Store(
            organization_id=org_id,
            name=f"__payroll_test_store_{suffix}",
            timezone="UTC",
            day_start_time={"all": "00:00"},
        )
        user = User(
            organization_id=org_id,
            role_id=seed_roles["staff"],
            username=f"__payroll_schema_test_{suffix}",
            full_name="Payroll Schema Test",
            password_hash="x",
            is_active=True,
        )
        db.add_all([store, user])
        await db.commit()
        await db.refresh(store)
        await db.refresh(user)
        store_id, user_id = store.id, user.id

    yield {"org_id": org_id, "store_id": store_id, "user_id": user_id}

    async with async_session() as db:
        await db.execute(delete(PayrollEvent).where(PayrollEvent.store_id == store_id))
        await db.execute(delete(PayrollEntry).where(PayrollEntry.store_id == store_id))
        await db.execute(delete(PayPeriod).where(PayPeriod.store_id == store_id))
        await db.execute(delete(Store).where(Store.id == store_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


def _period(payroll_env: dict, **overrides) -> PayPeriod:
    kwargs = dict(
        organization_id=payroll_env["org_id"],
        store_id=payroll_env["store_id"],
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 15),
    )
    kwargs.update(overrides)
    return PayPeriod(**kwargs)


def _entry(payroll_env: dict, period_id: UUID, **overrides) -> PayrollEntry:
    kwargs = dict(
        pay_period_id=period_id,
        organization_id=payroll_env["org_id"],
        store_id=payroll_env["store_id"],
        user_id=payroll_env["user_id"],
        member_name="Payroll Schema Test",
        calc_version=1,
        breakdown={"days": [], "rate_segments": [], "penalties": [], "tip_period_id": None},
    )
    kwargs.update(overrides)
    return PayrollEntry(**kwargs)


def _event(payroll_env: dict, **overrides) -> PayrollEvent:
    kwargs = dict(
        organization_id=payroll_env["org_id"],
        store_id=payroll_env["store_id"],
        user_id=payroll_env["user_id"],
        work_date=date(2026, 8, 3),
        kind="meal_penalty",
        reason="__test__ no meal break before 5th hour (worked 6h10m)",
    )
    kwargs.update(overrides)
    return PayrollEvent(**kwargs)


async def _fk_delete_rule(db, table: str, conname: str) -> str:
    return (
        await db.execute(
            text(
                "SELECT confdeltype::text FROM pg_constraint "
                f"WHERE conrelid = '{table}'::regclass AND conname = :name"
            ),
            {"name": conname},
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# 마이그레이션 스모크 — 스키마 객체 존재
# ---------------------------------------------------------------------------


async def test_schema_objects_exist(db) -> None:
    """테이블/인덱스/제약이 마이그레이션대로 존재한다."""
    tables = {
        row[0]
        for row in await db.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE tablename IN "
                "('pay_periods', 'payroll_entries', 'payroll_events')"
            )
        )
    }
    assert tables == {"pay_periods", "payroll_entries", "payroll_events"}

    # pay_periods — UNIQUE (store_id, start_date)
    pp_constraints = {
        row[0]
        for row in await db.execute(
            text("SELECT conname FROM pg_constraint WHERE conrelid = 'pay_periods'::regclass")
        )
    }
    assert "uq_pay_period_store_start" in pp_constraints

    # payroll_entries — partial unique (WHERE user_id IS NOT NULL)
    entry_indexdef = (
        await db.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'uq_payroll_entry_period_user_rev'"
            )
        )
    ).scalar_one()
    assert "UNIQUE" in entry_indexdef
    assert "user_id IS NOT NULL" in entry_indexdef

    # payroll_events — partial unique + 조회 인덱스 2종
    event_indexes = {
        row[0]
        for row in await db.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'payroll_events'")
        )
    }
    assert {
        "uq_payroll_event_user_date_kind",
        "ix_payroll_events_store_date",
        "ix_payroll_events_user_date",
    } <= event_indexes


async def test_fk_delete_rules(db) -> None:
    """FK delete rule: RESTRICT('r') / CASCADE('c') / SET NULL('n') 스펙 일치."""
    # 확정 급여 기록 보존 — store 삭제 금지
    assert await _fk_delete_rule(db, "pay_periods", "pay_periods_store_id_fkey") == "r"
    assert await _fk_delete_rule(db, "pay_periods", "pay_periods_organization_id_fkey") == "c"
    # entry 는 기간에 종속 (기간 삭제 시 같이 삭제)
    assert await _fk_delete_rule(db, "payroll_entries", "payroll_entries_pay_period_id_fkey") == "c"
    assert await _fk_delete_rule(db, "payroll_entries", "payroll_entries_user_id_fkey") == "n"
    assert await _fk_delete_rule(db, "payroll_entries", "payroll_entries_org_member_id_fkey") == "n"
    # 이벤트는 기간 삭제돼도 보존 (동결 해제만)
    assert await _fk_delete_rule(db, "payroll_events", "payroll_events_pay_period_id_fkey") == "n"
    assert await _fk_delete_rule(db, "payroll_events", "payroll_events_user_id_fkey") == "n"
    assert await _fk_delete_rule(db, "payroll_events", "payroll_events_attendance_id_fkey") == "n"


# ---------------------------------------------------------------------------
# pay_periods
# ---------------------------------------------------------------------------


async def test_pay_period_insert_defaults(payroll_env: dict) -> None:
    """기본 insert: status='open', created_at/updated_at 채워짐."""
    async with async_session() as db:
        period = _period(payroll_env)
        db.add(period)
        await db.commit()
        await db.refresh(period)

        assert period.status == "open"
        assert period.confirmed_at is None
        assert period.created_at is not None
        assert period.updated_at is not None


async def test_pay_period_duplicate_store_start_rejected(payroll_env: dict) -> None:
    """같은 (store, start_date) 두 번째 insert 거부 — end_date 가 달라도."""
    async with async_session() as db:
        db.add(_period(payroll_env))
        await db.commit()

    async with async_session() as db:
        db.add(_period(payroll_env, end_date=date(2026, 8, 31)))
        try:
            await db.commit()
            raise AssertionError("duplicate (store_id, start_date) must be rejected")
        except IntegrityError as exc:
            assert "uq_pay_period_store_start" in str(exc)
            await db.rollback()


async def test_store_delete_blocked_while_period_exists(payroll_env: dict) -> None:
    """기간이 있으면 store 삭제 차단(RESTRICT), 기간 삭제 후엔 허용."""
    async with async_session() as db:
        db.add(_period(payroll_env))
        await db.commit()

    async with async_session() as db:
        store = (
            await db.execute(select(Store).where(Store.id == payroll_env["store_id"]))
        ).scalar_one()
        await db.delete(store)
        try:
            await db.commit()
            raise AssertionError("store delete must be blocked by RESTRICT")
        except IntegrityError:
            await db.rollback()

    # 기간 제거 후 → store 삭제 성공 (fixture 정리와 동일 경로라 재생성해 둠)
    async with async_session() as db:
        await db.execute(
            delete(PayPeriod).where(PayPeriod.store_id == payroll_env["store_id"])
        )
        await db.commit()

    async with async_session() as db:
        store = (
            await db.execute(select(Store).where(Store.id == payroll_env["store_id"]))
        ).scalar_one()
        await db.delete(store)
        await db.commit()  # RESTRICT 해제 — 정상 삭제


# ---------------------------------------------------------------------------
# payroll_entries
# ---------------------------------------------------------------------------


async def test_entry_db_defaults_via_raw_insert(payroll_env: dict) -> None:
    """ORM default 를 우회한 raw INSERT 에서 revision/분/금액 DB default 0 확인."""
    async with async_session() as db:
        period = _period(payroll_env)
        db.add(period)
        await db.commit()
        period_id = period.id

    async with async_session() as db:
        row = (
            await db.execute(
                text(
                    "INSERT INTO payroll_entries "
                    "(id, pay_period_id, organization_id, store_id, user_id, "
                    " member_name, calc_version, breakdown) "
                    "VALUES (:id, :pid, :oid, :sid, :uid, :name, 1, CAST(:bd AS jsonb)) "
                    "RETURNING revision, regular_minutes, ot_minutes, dt_minutes, "
                    " regular_pay, ot_pay, dt_pay, penalty_pay, card_tips, gross_pay"
                ),
                {
                    "id": uuid_mod.uuid4(),
                    "pid": period_id,
                    "oid": payroll_env["org_id"],
                    "sid": payroll_env["store_id"],
                    "uid": payroll_env["user_id"],
                    "name": "Raw Default Test",
                    "bd": "{}",
                },
            )
        ).one()
        await db.commit()

        assert row.revision == 0
        assert (row.regular_minutes, row.ot_minutes, row.dt_minutes) == (0, 0, 0)
        for money in (
            row.regular_pay, row.ot_pay, row.dt_pay,
            row.penalty_pay, row.card_tips, row.gross_pay,
        ):
            assert money == Decimal("0")


async def test_entry_breakdown_not_null_enforced(payroll_env: dict) -> None:
    """breakdown SQL NULL 거부 (raw INSERT — ORM 은 None 을 JSON null 로 직렬화해서
    NOT NULL 에 안 걸리므로 DB 제약은 raw 로 확인)."""
    async with async_session() as db:
        period = _period(payroll_env)
        db.add(period)
        await db.commit()
        period_id = period.id

    async with async_session() as db:
        try:
            await db.execute(
                text(
                    "INSERT INTO payroll_entries "
                    "(id, pay_period_id, organization_id, store_id, user_id, "
                    " member_name, calc_version, breakdown) "
                    "VALUES (:id, :pid, :oid, :sid, :uid, :name, 1, NULL)"
                ),
                {
                    "id": uuid_mod.uuid4(),
                    "pid": period_id,
                    "oid": payroll_env["org_id"],
                    "sid": payroll_env["store_id"],
                    "uid": payroll_env["user_id"],
                    "name": "Null Breakdown Test",
                },
            )
            await db.commit()
            raise AssertionError("NULL breakdown must be rejected")
        except IntegrityError as exc:
            assert "breakdown" in str(exc)
            await db.rollback()


async def test_entry_money_decimal_roundtrip(payroll_env: dict) -> None:
    """Numeric(10,2) — Decimal 로 저장/조회, 센트 정밀도 유지."""
    async with async_session() as db:
        period = _period(payroll_env)
        db.add(period)
        await db.commit()

        entry = _entry(
            payroll_env,
            period.id,
            regular_minutes=480,
            regular_pay=Decimal("135.20"),
            gross_pay=Decimal("152.75"),
        )
        db.add(entry)
        await db.commit()
        entry_id = entry.id

    async with async_session() as db:
        fetched = (
            await db.execute(select(PayrollEntry).where(PayrollEntry.id == entry_id))
        ).scalar_one()
        assert isinstance(fetched.gross_pay, Decimal)
        assert fetched.regular_pay == Decimal("135.20")
        assert fetched.gross_pay == Decimal("152.75")
        assert fetched.revision == 0  # ORM default


async def test_entry_partial_unique_period_user_revision(payroll_env: dict) -> None:
    """(period, user, revision) 중복 거부 / revision 다르면 허용 / user NULL 은 예외."""
    async with async_session() as db:
        period = _period(payroll_env)
        db.add(period)
        await db.commit()
        period_id = period.id

    async with async_session() as db:
        db.add(_entry(payroll_env, period_id))
        await db.commit()

    # 같은 (period, user, revision=0) → 거부
    async with async_session() as db:
        db.add(_entry(payroll_env, period_id))
        try:
            await db.commit()
            raise AssertionError("duplicate (period, user, revision) must be rejected")
        except IntegrityError as exc:
            assert "uq_payroll_entry_period_user_rev" in str(exc)
            await db.rollback()

    # revision=1 (amendment 탈출구) → 허용
    async with async_session() as db:
        db.add(_entry(payroll_env, period_id, revision=1))
        await db.commit()

    # user_id NULL (고아 스냅샷) 은 partial 이라 여러 건 허용
    async with async_session() as db:
        db.add(_entry(payroll_env, period_id, user_id=None, member_name="Orphan A"))
        db.add(_entry(payroll_env, period_id, user_id=None, member_name="Orphan B"))
        await db.commit()

        count = len(
            (
                await db.execute(
                    select(PayrollEntry).where(PayrollEntry.pay_period_id == period_id)
                )
            ).scalars().all()
        )
        assert count == 4


async def test_entry_cascade_on_period_delete(payroll_env: dict) -> None:
    """pay_period 삭제 → entry 도 삭제 (CASCADE)."""
    async with async_session() as db:
        period = _period(payroll_env)
        db.add(period)
        await db.commit()
        db.add(_entry(payroll_env, period.id))
        await db.commit()
        period_id = period.id

    async with async_session() as db:
        await db.execute(delete(PayPeriod).where(PayPeriod.id == period_id))
        await db.commit()

        remaining = (
            await db.execute(
                select(PayrollEntry).where(PayrollEntry.pay_period_id == period_id)
            )
        ).scalars().all()
        assert remaining == []


# ---------------------------------------------------------------------------
# payroll_events
# ---------------------------------------------------------------------------


async def test_event_day_grain_unique(payroll_env: dict) -> None:
    """(org, user, work_date, kind) 하루 1건 / kind·날짜 다르면 공존 / user NULL 예외."""
    async with async_session() as db:
        db.add(_event(payroll_env))
        await db.commit()

    # 같은 날 같은 kind → 거부 (upsert 로만 갱신해야 함)
    async with async_session() as db:
        db.add(_event(payroll_env, reason="__test__ redetected"))
        try:
            await db.commit()
            raise AssertionError("duplicate (org, user, date, kind) must be rejected")
        except IntegrityError as exc:
            assert "uq_payroll_event_user_date_kind" in str(exc)
            await db.rollback()

    # 같은 날 다른 kind / 다른 날 같은 kind → 허용
    async with async_session() as db:
        db.add(_event(payroll_env, kind="daily_ot", reason="__test__ 9h on 8/3"))
        db.add(_event(payroll_env, work_date=date(2026, 8, 4)))
        await db.commit()

    # user NULL 은 partial 이라 중복 허용
    async with async_session() as db:
        db.add(_event(payroll_env, user_id=None))
        db.add(_event(payroll_env, user_id=None))
        await db.commit()

        count = len(
            (
                await db.execute(
                    select(PayrollEvent).where(
                        PayrollEvent.store_id == payroll_env["store_id"]
                    )
                )
            ).scalars().all()
        )
        assert count == 5


async def test_event_void_marking(payroll_env: dict) -> None:
    """조건 소멸 시 삭제 대신 voided_at 마킹 — 행은 보존."""
    async with async_session() as db:
        event = _event(payroll_env, attribution="management")
        db.add(event)
        await db.commit()
        event_id = event.id

    async with async_session() as db:
        event = (
            await db.execute(select(PayrollEvent).where(PayrollEvent.id == event_id))
        ).scalar_one()
        assert event.voided_at is None
        event.voided_at = datetime.now(timezone.utc)
        await db.commit()

    async with async_session() as db:
        event = (
            await db.execute(select(PayrollEvent).where(PayrollEvent.id == event_id))
        ).scalar_one()
        assert event.voided_at is not None
        assert event.attribution == "management"  # 태깅 보존


async def test_event_preserved_on_period_delete(payroll_env: dict) -> None:
    """pay_period 삭제 → 이벤트는 보존, pay_period_id 만 NULL (SET NULL)."""
    async with async_session() as db:
        period = _period(payroll_env)
        db.add(period)
        await db.commit()

        event = _event(payroll_env, pay_period_id=period.id)
        db.add(event)
        await db.commit()
        period_id, event_id = period.id, event.id

    async with async_session() as db:
        await db.execute(delete(PayPeriod).where(PayPeriod.id == period_id))
        await db.commit()

        event = (
            await db.execute(select(PayrollEvent).where(PayrollEvent.id == event_id))
        ).scalar_one()
        assert event.pay_period_id is None  # 동결 해제, 이벤트 자체는 보존
