"""attendances 자동퇴근 확인 컬럼 마이그레이션 스모크 테스트 — Payroll v1 Phase 3.

검증 대상 (스펙: docs/99_inbox/2026-08-03 payroll-v1-스키마-스펙.md §3,
마이그레이션 28f55a28c9f4):
    - auto_clock_out_confirmed_by (uuid NULL, FK users ON DELETE SET NULL)
    - auto_clock_out_confirmed_at (timestamptz NULL)
    - ORM insert/read 라운드트립 (기본 NULL → 확인 마킹 → 조회)
    - 확인자 user 삭제 시 SET NULL (attendance 는 보존)
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import date, datetime, timezone
from typing import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text

from app.database import async_session
from app.models.attendance import Attendance
from app.models.user import User


# ---------------------------------------------------------------------------
# 픽스처 — throwaway user 2명 (근무자 + 확인자).
# SET NULL 테스트가 확인자 삭제를 시도하므로 공용 시드 유저를 쓰면 안 됨.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def att_env(seed_organization: dict, seed_roles: dict[str, UUID]) -> AsyncIterator[dict]:
    """throwaway 근무자 + 확인자. 종료 시 attendance→user 순 정리."""
    org_id: UUID = seed_organization["id"]
    suffix = uuid_mod.uuid4().hex[:8]
    async with async_session() as db:
        worker = User(
            organization_id=org_id,
            role_id=seed_roles["staff"],
            username=f"__att_confirm_worker_{suffix}",
            full_name="Att Confirm Worker",
            password_hash="x",
            is_active=True,
        )
        confirmer = User(
            organization_id=org_id,
            role_id=seed_roles["supervisor"],
            username=f"__att_confirm_sv_{suffix}",
            full_name="Att Confirm SV",
            password_hash="x",
            is_active=True,
        )
        db.add_all([worker, confirmer])
        await db.commit()
        await db.refresh(worker)
        await db.refresh(confirmer)
        env = {"org_id": org_id, "worker_id": worker.id, "confirmer_id": confirmer.id}

    yield env

    async with async_session() as db:
        await db.execute(delete(Attendance).where(Attendance.user_id == env["worker_id"]))
        await db.execute(
            delete(User).where(User.id.in_([env["worker_id"], env["confirmer_id"]]))
        )
        await db.commit()


# ---------------------------------------------------------------------------
# 스키마 존재 검증
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_columns_exist_with_expected_types() -> None:
    """두 컬럼이 올바른 타입/NULL 허용으로 존재한다."""
    async with async_session() as db:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT column_name, data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_name = 'attendances'
                      AND column_name IN (
                        'auto_clock_out_confirmed_by',
                        'auto_clock_out_confirmed_at'
                      )
                    """
                )
            )
        ).all()
    cols = {r[0]: (r[1], r[2]) for r in rows}
    assert cols.get("auto_clock_out_confirmed_by") == ("uuid", "YES")
    assert cols.get("auto_clock_out_confirmed_at") == ("timestamp with time zone", "YES")


@pytest.mark.asyncio
async def test_fk_delete_rule_is_set_null() -> None:
    """confirmed_by FK 는 users 참조 + ON DELETE SET NULL."""
    async with async_session() as db:
        row = (
            await db.execute(
                text(
                    """
                    SELECT ccu.table_name AS ref_table, rc.delete_rule
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                    JOIN information_schema.referential_constraints rc
                      ON tc.constraint_name = rc.constraint_name
                    JOIN information_schema.constraint_column_usage ccu
                      ON rc.unique_constraint_name = ccu.constraint_name
                    WHERE tc.table_name = 'attendances'
                      AND tc.constraint_type = 'FOREIGN KEY'
                      AND kcu.column_name = 'auto_clock_out_confirmed_by'
                    """
                )
            )
        ).one_or_none()
    assert row is not None, "auto_clock_out_confirmed_by FK missing"
    assert row[0] == "users"
    assert row[1] == "SET NULL"


# ---------------------------------------------------------------------------
# ORM 라운드트립
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orm_roundtrip_default_null_then_confirm(att_env: dict) -> None:
    """기본값 NULL → 확인 마킹 저장 → 재조회 시 값 유지."""
    confirmed_at = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    async with async_session() as db:
        att = Attendance(
            organization_id=att_env["org_id"],
            user_id=att_env["worker_id"],
            work_date=date(2026, 8, 1),
            status="clocked_out",
        )
        db.add(att)
        await db.commit()
        await db.refresh(att)
        att_id = att.id
        # 기본값은 NULL (미확인)
        assert att.auto_clock_out_confirmed_by is None
        assert att.auto_clock_out_confirmed_at is None

        att.auto_clock_out_confirmed_by = att_env["confirmer_id"]
        att.auto_clock_out_confirmed_at = confirmed_at
        await db.commit()

    async with async_session() as db:
        fetched = (
            await db.execute(select(Attendance).where(Attendance.id == att_id))
        ).scalar_one()
        assert fetched.auto_clock_out_confirmed_by == att_env["confirmer_id"]
        assert fetched.auto_clock_out_confirmed_at == confirmed_at


@pytest.mark.asyncio
async def test_confirmer_delete_sets_null_and_preserves_attendance(att_env: dict) -> None:
    """확인자 user 삭제 시 confirmed_by 만 NULL, attendance/confirmed_at 은 보존."""
    confirmed_at = datetime(2026, 8, 2, 9, 30, tzinfo=timezone.utc)
    async with async_session() as db:
        att = Attendance(
            organization_id=att_env["org_id"],
            user_id=att_env["worker_id"],
            work_date=date(2026, 8, 2),
            status="clocked_out",
            auto_clock_out_confirmed_by=att_env["confirmer_id"],
            auto_clock_out_confirmed_at=confirmed_at,
        )
        db.add(att)
        await db.commit()
        att_id = att.id

    async with async_session() as db:
        await db.execute(delete(User).where(User.id == att_env["confirmer_id"]))
        await db.commit()

    async with async_session() as db:
        fetched = (
            await db.execute(select(Attendance).where(Attendance.id == att_id))
        ).scalar_one()
        assert fetched.auto_clock_out_confirmed_by is None  # SET NULL
        assert fetched.auto_clock_out_confirmed_at == confirmed_at  # 시각은 보존
