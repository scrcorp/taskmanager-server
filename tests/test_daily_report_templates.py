"""Daily report template — repository fallback 테스트.

대상: DailyReportTemplateRepository.get_template_for_store (다중 active 안전망)

SWC 매장처럼 한 매장에 active 템플릿이 2개 이상 있어도 scalar_one_or_none() 에서
MultipleResultsFound 가 나지 않고 default 우선 1개를 반환해야 한다.
"""

from __future__ import annotations

from typing import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.database import async_session
from app.models.daily_report import DailyReportTemplate
from app.repositories.daily_report_repository import daily_report_template_repository


@pytest_asyncio.fixture
async def _tracked_template_ids() -> AsyncIterator[list[UUID]]:
    ids: list[UUID] = []
    yield ids
    if ids:
        async with async_session() as db:
            await db.execute(
                delete(DailyReportTemplate).where(DailyReportTemplate.id.in_(ids))
            )
            await db.commit()


@pytest.mark.asyncio
async def test_repository_picks_default_when_multiple_active(
    test_users: dict, test_store_id: UUID, _tracked_template_ids: list[UUID]
):
    org_id = test_users["testadmin"]["organization_id"]

    async with async_session() as db:
        t1 = DailyReportTemplate(
            organization_id=org_id, store_id=test_store_id,
            name="A non-default", is_default=False, is_active=True,
        )
        t2 = DailyReportTemplate(
            organization_id=org_id, store_id=test_store_id,
            name="B default", is_default=True, is_active=True,
        )
        db.add_all([t1, t2])
        await db.commit()
        await db.refresh(t1); await db.refresh(t2)
        _tracked_template_ids.extend([t1.id, t2.id])

    async with async_session() as db:
        picked = await daily_report_template_repository.get_template_for_store(
            db, org_id, test_store_id
        )
        assert picked is not None
        assert picked.id == t2.id
