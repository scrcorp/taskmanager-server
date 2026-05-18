"""Daily report template — repository fallback + service guard 테스트.

대상:
- DailyReportTemplateRepository.get_template_for_store (다중 active 안전망)
- DailyReportService default exclusivity + 마지막 active 보호 가드

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
from app.schemas.daily_report import (
    DailyReportTemplateCreate,
    DailyReportTemplateSectionInput,
    DailyReportTemplateUpdate,
)
from app.services.daily_report_service import daily_report_service
from app.utils.exceptions import BadRequestError


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


def _make_section() -> DailyReportTemplateSectionInput:
    return DailyReportTemplateSectionInput(
        title="Section 1", description=None, is_required=False
    )


@pytest.mark.asyncio
async def test_create_template_default_demotes_existing_default(
    test_users: dict, test_store_id: UUID, _tracked_template_ids: list[UUID]
):
    """create_template 으로 is_default=True 만들면 같은 scope 기존 default 가 false."""
    org_id = test_users["testadmin"]["organization_id"]

    async with async_session() as db:
        existing = DailyReportTemplate(
            organization_id=org_id, store_id=test_store_id,
            name="Existing default", is_default=True, is_active=True,
        )
        db.add(existing)
        await db.commit()
        await db.refresh(existing)
        _tracked_template_ids.append(existing.id)

    async with async_session() as db:
        created = await daily_report_service.create_template(
            db, org_id,
            DailyReportTemplateCreate(
                name="New default", store_id=str(test_store_id),
                is_default=True, sections=[_make_section()],
            ),
        )
        new_id: UUID = created.id  # service returns ORM model — UUID
        _tracked_template_ids.append(new_id)

    async with async_session() as db:
        refreshed = await db.get(DailyReportTemplate, existing.id)
        assert refreshed is not None
        assert refreshed.is_default is False
        new_tpl = await db.get(DailyReportTemplate, new_id)
        assert new_tpl is not None and new_tpl.is_default is True


@pytest.mark.asyncio
async def test_update_template_promote_to_default_demotes_others(
    test_users: dict, test_store_id: UUID, _tracked_template_ids: list[UUID]
):
    """update_template 로 promote 시 같은 scope 기존 default 가 false."""
    org_id = test_users["testadmin"]["organization_id"]

    async with async_session() as db:
        current_default = DailyReportTemplate(
            organization_id=org_id, store_id=test_store_id,
            name="Current default", is_default=True, is_active=True,
        )
        other = DailyReportTemplate(
            organization_id=org_id, store_id=test_store_id,
            name="Other", is_default=False, is_active=True,
        )
        db.add_all([current_default, other])
        await db.commit()
        await db.refresh(current_default); await db.refresh(other)
        _tracked_template_ids.extend([current_default.id, other.id])

    async with async_session() as db:
        await daily_report_service.update_template(
            db, other.id, org_id,
            DailyReportTemplateUpdate(is_default=True),
        )

    async with async_session() as db:
        refreshed_current = await db.get(DailyReportTemplate, current_default.id)
        refreshed_other = await db.get(DailyReportTemplate, other.id)
        assert refreshed_current is not None and refreshed_current.is_default is False
        assert refreshed_other is not None and refreshed_other.is_default is True


@pytest.mark.asyncio
async def test_update_template_cannot_deactivate_last_active(
    test_users: dict, second_store_id: UUID, _tracked_template_ids: list[UUID]
):
    """같은 scope 의 마지막 active 를 inactive 로 바꾸려 하면 BadRequestError."""
    org_id = test_users["testadmin"]["organization_id"]

    async with async_session() as db:
        only = DailyReportTemplate(
            organization_id=org_id, store_id=second_store_id,
            name="Only active in scope", is_default=False, is_active=True,
        )
        db.add(only)
        await db.commit()
        await db.refresh(only)
        _tracked_template_ids.append(only.id)

    async with async_session() as db:
        with pytest.raises(BadRequestError):
            await daily_report_service.update_template(
                db, only.id, org_id,
                DailyReportTemplateUpdate(is_active=False),
            )
