from datetime import date
from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.daily_report import (
    DailyReport,
    DailyReportComment,
    DailyReportTemplate,
    DailyReportTemplateSection,
)
from app.repositories.base import BaseRepository


class DailyReportRepository(BaseRepository[DailyReport]):
    def __init__(self) -> None:
        super().__init__(DailyReport)

    async def get_with_details(
        self, db: AsyncSession, report_id: UUID, organization_id: UUID
    ) -> DailyReport | None:
        query: Select = (
            select(DailyReport)
            .options(
                selectinload(DailyReport.sections),
                selectinload(DailyReport.comments),
            )
            .where(
                DailyReport.id == report_id,
                DailyReport.organization_id == organization_id,
            )
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_by_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        author_id: UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        period: str | None = None,
        status: str | None = None,
        exclude_status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[DailyReport], int]:
        base = select(DailyReport).where(DailyReport.organization_id == organization_id)
        if store_id:
            base = base.where(DailyReport.store_id == store_id)
        if author_id:
            base = base.where(DailyReport.author_id == author_id)
        if date_from:
            base = base.where(DailyReport.report_date >= date_from)
        if date_to:
            base = base.where(DailyReport.report_date <= date_to)
        if period:
            base = base.where(DailyReport.period == period)
        if status:
            base = base.where(DailyReport.status == status)
        if exclude_status:
            base = base.where(DailyReport.status != exclude_status)

        count_result = await db.execute(select(func.count()).select_from(base.subquery()))
        total: int = count_result.scalar() or 0

        query = (
            base.options(selectinload(DailyReport.comments))
            .order_by(DailyReport.report_date.desc(), DailyReport.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        result = await db.execute(query)
        return list(result.scalars().all()), total

    async def check_duplicate(
        self, db: AsyncSession, store_id: UUID, report_date: date, period: str
    ) -> bool:
        result = await db.execute(
            select(func.count()).where(
                DailyReport.store_id == store_id,
                DailyReport.report_date == report_date,
                DailyReport.period == period,
            )
        )
        return (result.scalar() or 0) > 0


class DailyReportTemplateRepository:
    async def get_template_for_store(
        self, db: AsyncSession, organization_id: UUID, store_id: UUID | None = None
    ) -> DailyReportTemplate | None:
        # 1. Store-specific template
        if store_id:
            query = (
                select(DailyReportTemplate)
                .options(selectinload(DailyReportTemplate.sections))
                .where(
                    DailyReportTemplate.store_id == store_id,
                    DailyReportTemplate.is_active == True,
                )
            )
            result = await db.execute(query)
            template = result.scalar_one_or_none()
            if template:
                return template

        # 2. Org-level template
        query = (
            select(DailyReportTemplate)
            .options(selectinload(DailyReportTemplate.sections))
            .where(
                DailyReportTemplate.organization_id == organization_id,
                DailyReportTemplate.store_id == None,
                DailyReportTemplate.is_active == True,
            )
        )
        result = await db.execute(query)
        template = result.scalar_one_or_none()
        if template:
            return template

        # 3. System default
        query = (
            select(DailyReportTemplate)
            .options(selectinload(DailyReportTemplate.sections))
            .where(
                DailyReportTemplate.organization_id == None,
                DailyReportTemplate.is_default == True,
                DailyReportTemplate.is_active == True,
            )
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_by_id_with_sections(
        self, db: AsyncSession, template_id: UUID
    ) -> DailyReportTemplate | None:
        query = (
            select(DailyReportTemplate)
            .options(selectinload(DailyReportTemplate.sections))
            .where(DailyReportTemplate.id == template_id)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()


daily_report_repository: DailyReportRepository = DailyReportRepository()
daily_report_template_repository: DailyReportTemplateRepository = DailyReportTemplateRepository()
