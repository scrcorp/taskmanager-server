"""hourly_rate_history 모델/마이그레이션 스모크 테스트 — Payroll v1 Phase 1.

검증 대상 (스펙: docs/99_inbox/2026-08-03 payroll-v1-스키마-스펙.md §1, §2):
    - 테이블/인덱스/제약 존재 (마이그레이션 b77039450fd5 적용 확인)
    - insert 기본 동작 (old_rate NULL = 최초 기록, created_at server default)
    - uq_rate_history_member_effective: 같은 (org_member, effective_date) 중복 거부
    - ck_rate_history_new_rate_positive: new_rate <= 0 거부
    - org_member FK RESTRICT: 이력이 있으면 멤버 삭제 차단, 이력 삭제 후엔 허용
    - org_members.hire_date / termination_date 컬럼 동작
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import date
from decimal import Decimal
from typing import AsyncIterator
from uuid import UUID

import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.models.org_member import OrgMember
from app.models.rate import HourlyRateHistory
from app.models.user import User


# ---------------------------------------------------------------------------
# 픽스처 — 테스트 전용 throwaway user + org_member (다른 테스트 시드와 격리)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def payroll_member(seed_organization: dict, seed_roles: dict[str, UUID]) -> AsyncIterator[dict]:
    """이력 테스트 전용 user + org_member. 종료 시 이력→멤버→유저 순 정리.

    seed 유저(teststaff 등)의 org_member 를 쓰지 않는 이유:
    RESTRICT 테스트가 멤버 삭제를 시도하므로 공용 시드를 오염시키면 안 됨.
    """
    org_id: UUID = seed_organization["id"]
    suffix = uuid_mod.uuid4().hex[:8]
    async with async_session() as db:
        user = User(
            organization_id=org_id,
            role_id=seed_roles["staff"],
            username=f"__payroll_rate_test_{suffix}",
            full_name="Payroll Rate Test",
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
        )
        db.add(member)
        await db.commit()
        await db.refresh(member)
        user_id, member_id = user.id, member.id

    yield {"user_id": user_id, "member_id": member_id, "org_id": org_id}

    async with async_session() as db:
        await db.execute(
            delete(HourlyRateHistory).where(HourlyRateHistory.org_member_id == member_id)
        )
        await db.execute(delete(OrgMember).where(OrgMember.id == member_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


def _history_row(payroll_member: dict, **overrides) -> HourlyRateHistory:
    """기본값 채운 이력 행 팩토리."""
    kwargs = dict(
        organization_id=payroll_member["org_id"],
        org_member_id=payroll_member["member_id"],
        old_rate=None,
        new_rate=Decimal("18.50"),
        effective_date=date(2026, 8, 1),
        reason="__test__ initial rate",
    )
    kwargs.update(overrides)
    return HourlyRateHistory(**kwargs)


# ---------------------------------------------------------------------------
# 마이그레이션 스모크 — 스키마 객체 존재
# ---------------------------------------------------------------------------


async def test_schema_objects_exist(db) -> None:
    """테이블/인덱스/제약이 마이그레이션대로 존재한다."""
    indexes = {
        row[0]
        for row in await db.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'hourly_rate_history'")
        )
    }
    assert "uq_rate_history_member_effective" in indexes
    assert "ix_rate_history_member_effective" in indexes
    assert "ix_hourly_rate_history_organization_id" in indexes

    constraints = {
        row[0]
        for row in await db.execute(
            text("SELECT conname FROM pg_constraint WHERE conrelid = 'hourly_rate_history'::regclass")
        )
    }
    assert "ck_rate_history_new_rate_positive" in constraints

    # org_member FK 는 RESTRICT (confdeltype 'r')
    delete_rule = (
        await db.execute(
            text(
                "SELECT confdeltype::text FROM pg_constraint "
                "WHERE conrelid = 'hourly_rate_history'::regclass "
                "AND conname = 'hourly_rate_history_org_member_id_fkey'"
            )
        )
    ).scalar_one()
    assert delete_rule == "r"

    org_member_cols = {
        row[0]
        for row in await db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'org_members' "
                "AND column_name IN ('hire_date', 'termination_date')"
            )
        )
    }
    assert org_member_cols == {"hire_date", "termination_date"}


# ---------------------------------------------------------------------------
# insert 기본 동작
# ---------------------------------------------------------------------------


async def test_insert_first_record(payroll_member: dict) -> None:
    """최초 기록: old_rate NULL 허용, created_at server default 채워짐."""
    async with async_session() as db:
        row = _history_row(payroll_member)
        db.add(row)
        await db.commit()
        await db.refresh(row)

        assert row.old_rate is None
        assert row.new_rate == Decimal("18.50")
        assert row.created_at is not None

        # Decimal 로 읽힌다 (Numeric(10,2) ORM 매핑)
        fetched = (
            await db.execute(
                select(HourlyRateHistory).where(HourlyRateHistory.id == row.id)
            )
        ).scalar_one()
        assert isinstance(fetched.new_rate, Decimal)


async def test_insert_subsequent_record_with_old_rate(payroll_member: dict) -> None:
    """이후 기록: old_rate 채움 + 다른 effective_date 는 공존 가능."""
    async with async_session() as db:
        db.add(_history_row(payroll_member, effective_date=date(2026, 8, 1)))
        db.add(
            _history_row(
                payroll_member,
                old_rate=Decimal("18.50"),
                new_rate=Decimal("20.00"),
                effective_date=date(2026, 9, 1),
                reason="__test__ raise",
            )
        )
        await db.commit()

        count = len(
            (
                await db.execute(
                    select(HourlyRateHistory).where(
                        HourlyRateHistory.org_member_id == payroll_member["member_id"]
                    )
                )
            ).scalars().all()
        )
        assert count == 2


# ---------------------------------------------------------------------------
# 제약: UNIQUE (org_member_id, effective_date)
# ---------------------------------------------------------------------------


async def test_duplicate_member_effective_date_rejected(payroll_member: dict) -> None:
    """같은 (멤버, 적용일) 두 번째 insert 는 거부 — 재등록은 update 로 해야 함."""
    async with async_session() as db:
        db.add(_history_row(payroll_member))
        await db.commit()

    async with async_session() as db:
        db.add(_history_row(payroll_member, new_rate=Decimal("19.00")))
        try:
            await db.commit()
            raise AssertionError("duplicate (org_member_id, effective_date) must be rejected")
        except IntegrityError as exc:
            assert "uq_rate_history_member_effective" in str(exc)
            await db.rollback()


# ---------------------------------------------------------------------------
# 제약: CHECK new_rate > 0
# ---------------------------------------------------------------------------


async def test_new_rate_zero_rejected(payroll_member: dict) -> None:
    async with async_session() as db:
        db.add(_history_row(payroll_member, new_rate=Decimal("0")))
        try:
            await db.commit()
            raise AssertionError("new_rate = 0 must be rejected")
        except IntegrityError as exc:
            assert "ck_rate_history_new_rate_positive" in str(exc)
            await db.rollback()


async def test_new_rate_negative_rejected(payroll_member: dict) -> None:
    async with async_session() as db:
        db.add(_history_row(payroll_member, new_rate=Decimal("-5.00")))
        try:
            await db.commit()
            raise AssertionError("negative new_rate must be rejected")
        except IntegrityError as exc:
            assert "ck_rate_history_new_rate_positive" in str(exc)
            await db.rollback()


# ---------------------------------------------------------------------------
# 제약: org_member FK RESTRICT — 급여 근거 소멸 금지
# ---------------------------------------------------------------------------


async def test_member_delete_blocked_while_history_exists(payroll_member: dict) -> None:
    """이력이 있으면 org_member 삭제 차단, 이력 삭제 후엔 삭제 가능."""
    async with async_session() as db:
        db.add(_history_row(payroll_member))
        await db.commit()

    # 이력 존재 → 멤버 삭제 시도 = IntegrityError (RESTRICT)
    async with async_session() as db:
        member = (
            await db.execute(
                select(OrgMember).where(OrgMember.id == payroll_member["member_id"])
            )
        ).scalar_one()
        await db.delete(member)
        try:
            await db.commit()
            raise AssertionError("org_member delete must be blocked by RESTRICT")
        except IntegrityError:
            await db.rollback()

    # 이력 제거 후 → 멤버 삭제 성공
    async with async_session() as db:
        await db.execute(
            delete(HourlyRateHistory).where(
                HourlyRateHistory.org_member_id == payroll_member["member_id"]
            )
        )
        await db.commit()

    async with async_session() as db:
        member = (
            await db.execute(
                select(OrgMember).where(OrgMember.id == payroll_member["member_id"])
            )
        ).scalar_one()
        await db.delete(member)
        await db.commit()  # RESTRICT 해제 — 정상 삭제

        gone = (
            await db.execute(
                select(OrgMember).where(OrgMember.id == payroll_member["member_id"])
            )
        ).scalar_one_or_none()
        assert gone is None


# ---------------------------------------------------------------------------
# org_members 신규 컬럼: hire_date / termination_date
# ---------------------------------------------------------------------------


async def test_org_member_hr_dates_roundtrip(payroll_member: dict) -> None:
    """hire_date / termination_date 저장·조회 (NULL 기본 → 값 설정)."""
    async with async_session() as db:
        member = (
            await db.execute(
                select(OrgMember).where(OrgMember.id == payroll_member["member_id"])
            )
        ).scalar_one()
        assert member.hire_date is None
        assert member.termination_date is None

        member.hire_date = date(2026, 1, 15)
        member.termination_date = date(2026, 12, 31)
        await db.commit()

    async with async_session() as db:
        member = (
            await db.execute(
                select(OrgMember).where(OrgMember.id == payroll_member["member_id"])
            )
        ).scalar_one()
        assert member.hire_date == date(2026, 1, 15)
        assert member.termination_date == date(2026, 12, 31)
