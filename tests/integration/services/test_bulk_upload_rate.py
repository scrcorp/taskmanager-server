"""Integration — bulk_upload 직원 CSV 의 hourly_rate 가 단일 mutation 경로를 탄다.

대상: bulk_upload_service._process_employee_rows (Payroll v1 Phase 1 쓰기 경로 통일)

검증:
    - CSV hourly_rate → hourly_rate_history 생성 + org_members(canonical)/users(미러)
    - hourly_rate 빈 값이면 이력 미생성 (org default 자동 채움만 — 생성 경로)
"""

from __future__ import annotations

import uuid as uuid_mod
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models.org_member import OrgMember
from app.models.organization import Store
from app.models.rate import HourlyRateHistory
from app.models.user import User
from app.services.bulk_upload_service import bulk_upload_service

pytestmark = pytest.mark.asyncio


async def _cleanup_user(username: str) -> None:
    async with async_session() as db:
        user_id = await db.scalar(select(User.id).where(User.username == username))
        if user_id is None:
            return
        member_ids = (
            await db.execute(
                select(OrgMember.id).where(OrgMember.user_id == user_id)
            )
        ).scalars().all()
        if member_ids:
            await db.execute(
                delete(HourlyRateHistory).where(
                    HourlyRateHistory.org_member_id.in_(member_ids)
                )
            )
        # org_members / user_stores 는 users FK CASCADE 로 함께 삭제
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def _load_caller(admin_id: UUID) -> User:
    async with async_session() as db:
        return (
            await db.execute(
                select(User)
                .options(selectinload(User.role))
                .where(User.id == admin_id)
            )
        ).scalar_one()


async def test_bulk_upload_rate_goes_through_history(
    seed_organization: dict, test_users: dict, test_store_id: UUID,
) -> None:
    """CSV hourly_rate → 이력 + org_members/users dual-write."""
    org_id = seed_organization["id"]
    # username 은 영숫자 시작 규칙 — 테스트 식별용 접두어는 'z' 로 시작
    username = f"zbulkrate{uuid_mod.uuid4().hex[:8]}"
    async with async_session() as db:
        store_name = await db.scalar(select(Store.name).where(Store.id == test_store_id))

    csv_text = (
        "username,password,full_name,role,store_name,email,hourly_rate\n"
        f"{username},pw1234,Bulk Rate Test,staff,{store_name},,19.50\n"
    )
    caller = await _load_caller(test_users["testadmin"]["id"])

    try:
        async with async_session() as db:
            result = await bulk_upload_service.process_employees(
                db, org_id, csv_text.encode(), caller, filename="employees.csv",
            )
        assert result["created"] == 1, result

        async with async_session() as db:
            user = (
                await db.execute(select(User).where(User.username == username))
            ).scalar_one()
            member = (
                await db.execute(
                    select(OrgMember).where(
                        OrgMember.user_id == user.id,
                        OrgMember.organization_id == org_id,
                    )
                )
            ).scalar_one()
            rows = (
                await db.execute(
                    select(HourlyRateHistory).where(
                        HourlyRateHistory.org_member_id == member.id
                    )
                )
            ).scalars().all()

            assert Decimal(member.hourly_rate) == Decimal("19.50")  # canonical
            assert Decimal(user.hourly_rate) == Decimal("19.50")  # 미러
            assert len(rows) == 1
            assert rows[0].new_rate == Decimal("19.50")
            assert rows[0].reason == "Imported via bulk upload"
            assert rows[0].changed_by == caller.id
    finally:
        await _cleanup_user(username)


async def test_bulk_upload_without_rate_no_history(
    seed_organization: dict, test_users: dict, test_store_id: UUID,
) -> None:
    """hourly_rate 빈 값 → 이력 없음 (생성 시 org default 자동 채움은 이력화 안 함)."""
    org_id = seed_organization["id"]
    username = f"zbulknorate{uuid_mod.uuid4().hex[:8]}"
    async with async_session() as db:
        store_name = await db.scalar(select(Store.name).where(Store.id == test_store_id))

    csv_text = (
        "username,password,full_name,role,store_name,email,hourly_rate\n"
        f"{username},pw1234,Bulk NoRate Test,staff,{store_name},,\n"
    )
    caller = await _load_caller(test_users["testadmin"]["id"])

    try:
        async with async_session() as db:
            result = await bulk_upload_service.process_employees(
                db, org_id, csv_text.encode(), caller, filename="employees.csv",
            )
        assert result["created"] == 1, result

        async with async_session() as db:
            user = (
                await db.execute(select(User).where(User.username == username))
            ).scalar_one()
            member = (
                await db.execute(
                    select(OrgMember).where(
                        OrgMember.user_id == user.id,
                        OrgMember.organization_id == org_id,
                    )
                )
            ).scalar_one()
            count = len(
                (
                    await db.execute(
                        select(HourlyRateHistory).where(
                            HourlyRateHistory.org_member_id == member.id
                        )
                    )
                ).scalars().all()
            )
        assert count == 0
    finally:
        await _cleanup_user(username)
