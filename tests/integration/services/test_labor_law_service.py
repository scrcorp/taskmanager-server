"""Integration tests — resolve_weekly_max_hours (P0-3, BLL-2).

LaborLawSetting 을 매장별로 resolve 하는지 검증:
    - 매장 A/B 각각 자기 설정 적용 (org 내 임의 row 적용 버그 방지)
    - cascade: store_max_weekly > state_max_weekly > federal_max_weekly
    - row 없음 → 기본 40
"""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.database import async_session
from app.models.organization import LaborLawSetting
from app.services.labor_law_service import (
    DEFAULT_MAX_WEEKLY_HOURS,
    resolve_weekly_max_hours,
)


pytestmark = pytest.mark.asyncio


async def _delete_settings(store_ids: list[UUID]) -> None:
    async with async_session() as db:
        await db.execute(
            delete(LaborLawSetting).where(LaborLawSetting.store_id.in_(store_ids))
        )
        await db.commit()


@pytest_asyncio.fixture
async def clean_labor_settings(test_store_id: UUID, second_store_id: UUID):
    """두 테스트 매장의 LaborLawSetting 을 테스트 전후 제거."""
    await _delete_settings([test_store_id, second_store_id])
    yield
    await _delete_settings([test_store_id, second_store_id])


async def _create_setting(
    org_id: UUID,
    store_id: UUID,
    *,
    federal: int = 40,
    state: int | None = None,
    store_max: int | None = None,
) -> None:
    async with async_session() as db:
        db.add(
            LaborLawSetting(
                organization_id=org_id,
                store_id=store_id,
                federal_max_weekly=federal,
                state_max_weekly=state,
                store_max_weekly=store_max,
            )
        )
        await db.commit()


async def test_two_stores_resolve_their_own_settings(
    clean_labor_settings,
    seed_organization: dict,
    test_store_id: UUID,
    second_store_id: UUID,
    db,
) -> None:
    """매장 A=30, 매장 B=45 — 각자 자기 설정으로 resolve (임의 row 적용 금지)."""
    org_id = seed_organization["id"]
    await _create_setting(org_id, test_store_id, store_max=30)
    await _create_setting(org_id, second_store_id, store_max=45)

    assert await resolve_weekly_max_hours(db, test_store_id) == 30
    assert await resolve_weekly_max_hours(db, second_store_id) == 45


async def test_cascade_store_over_state_over_federal(
    clean_labor_settings,
    seed_organization: dict,
    test_store_id: UUID,
    db,
) -> None:
    """store_max 가 있으면 state/federal 무시."""
    await _create_setting(
        seed_organization["id"], test_store_id, federal=48, state=44, store_max=35
    )
    assert await resolve_weekly_max_hours(db, test_store_id) == 35


async def test_cascade_state_when_store_none(
    clean_labor_settings,
    seed_organization: dict,
    test_store_id: UUID,
    db,
) -> None:
    """store_max None → state_max 적용."""
    await _create_setting(
        seed_organization["id"], test_store_id, federal=48, state=44, store_max=None
    )
    assert await resolve_weekly_max_hours(db, test_store_id) == 44


async def test_cascade_federal_when_store_and_state_none(
    clean_labor_settings,
    seed_organization: dict,
    test_store_id: UUID,
    db,
) -> None:
    """store/state None → federal 적용."""
    await _create_setting(
        seed_organization["id"], test_store_id, federal=48, state=None, store_max=None
    )
    assert await resolve_weekly_max_hours(db, test_store_id) == 48


async def test_default_40_when_no_row(
    clean_labor_settings,
    test_store_id: UUID,
    db,
) -> None:
    """해당 매장 row 없음 → 기본 40."""
    assert await resolve_weekly_max_hours(db, test_store_id) == DEFAULT_MAX_WEEKLY_HOURS
    assert DEFAULT_MAX_WEEKLY_HOURS == 40


async def test_other_store_setting_does_not_leak(
    clean_labor_settings,
    seed_organization: dict,
    test_store_id: UUID,
    second_store_id: UUID,
    db,
) -> None:
    """다른 매장에만 설정이 있으면 내 매장은 기본 40 (BLL-2 회귀 방지)."""
    await _create_setting(seed_organization["id"], second_store_id, store_max=25)
    assert await resolve_weekly_max_hours(db, test_store_id) == DEFAULT_MAX_WEEKLY_HOURS
