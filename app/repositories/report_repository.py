"""Repository for unified multi-type Report."""
from datetime import date
from typing import Sequence
from uuid import UUID

from sqlalchemy import Date, Select, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.report import Report, ReportTemplate
from app.repositories.base import BaseRepository


class ReportRepository(BaseRepository[Report]):
    def __init__(self) -> None:
        super().__init__(Report)

    async def get_with_details(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
    ) -> Report | None:
        query: Select = (
            select(Report)
            .options(selectinload(Report.comments))
            .where(
                Report.id == report_id,
                Report.organization_id == organization_id,
                Report.deleted_at.is_(None),
            )
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_by_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
        *,
        type: str | None = None,
        store_id: UUID | None = None,
        author_id: UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        status: str | None = None,
        exclude_status: str | None = None,
        payload_filters: dict | None = None,
        extra_clause=None,
        page: int = 1,
        per_page: int = 20,
        accessible_store_ids: list[UUID] | None = None,
    ) -> tuple[Sequence[Report], int]:
        base = (
            select(Report)
            .where(Report.organization_id == organization_id)
            .where(Report.deleted_at.is_(None))
        )
        if type:
            base = base.where(Report.type == type)
        if accessible_store_ids is not None:
            if not accessible_store_ids:
                return [], 0
            base = base.where(Report.store_id.in_(accessible_store_ids))
        if store_id:
            base = base.where(Report.store_id == store_id)
        if author_id:
            base = base.where(Report.author_id == author_id)
        # date filter — issue 등 report_date 가 null 인 타입도 매칭되도록
        # COALESCE(report_date, created_at::date) 로 비교.
        if date_from or date_to:
            effective_date = func.coalesce(
                Report.report_date, cast(Report.created_at, Date)
            )
            if date_from:
                base = base.where(effective_date >= date_from)
            if date_to:
                base = base.where(effective_date <= date_to)
        if status:
            base = base.where(Report.status == status)
        if exclude_status:
            base = base.where(Report.status != exclude_status)
        if payload_filters:
            for k, v in payload_filters.items():
                base = base.where(Report.payload[k].astext == str(v))
        if extra_clause is not None:
            base = base.where(extra_clause)

        count_result = await db.execute(select(func.count()).select_from(base.subquery()))
        total: int = count_result.scalar() or 0

        query = (
            base.options(selectinload(Report.comments))
            .order_by(Report.report_date.desc().nulls_last(), Report.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        result = await db.execute(query)
        return list(result.scalars().all()), total

    async def find_daily_duplicate(
        self,
        db: AsyncSession,
        store_id: UUID,
        report_date: date,
        period: str,
    ) -> Report | None:
        result = await db.execute(
            select(Report).where(
                Report.type == "daily",
                Report.store_id == store_id,
                Report.report_date == report_date,
                Report.payload["period"].astext == period,
                Report.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()


class ReportTemplateRepository:
    async def get_by_id(
        self, db: AsyncSession, template_id: UUID
    ) -> ReportTemplate | None:
        result = await db.execute(select(ReportTemplate).where(ReportTemplate.id == template_id))
        return result.scalar_one_or_none()

    async def get_template_for_store(
        self,
        db: AsyncSession,
        type: str,
        organization_id: UUID,
        store_id: UUID | None = None,
    ) -> ReportTemplate | None:
        # 1. Store-specific
        if store_id:
            result = await db.execute(
                select(ReportTemplate).where(
                    ReportTemplate.type == type,
                    ReportTemplate.store_id == store_id,
                    ReportTemplate.is_active.is_(True),
                )
            )
            t = result.scalar_one_or_none()
            if t:
                return t

        # 2. Org-level
        result = await db.execute(
            select(ReportTemplate).where(
                ReportTemplate.type == type,
                ReportTemplate.organization_id == organization_id,
                ReportTemplate.store_id.is_(None),
                ReportTemplate.is_active.is_(True),
            )
        )
        t = result.scalar_one_or_none()
        if t:
            return t

        # 3. System default
        result = await db.execute(
            select(ReportTemplate).where(
                ReportTemplate.type == type,
                ReportTemplate.organization_id.is_(None),
                ReportTemplate.is_default.is_(True),
                ReportTemplate.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    async def list_for_org(
        self,
        db: AsyncSession,
        type: str | None,
        organization_id: UUID,
        store_id: UUID | None = None,
        is_active: bool | None = None,
    ) -> list[ReportTemplate]:
        q = select(ReportTemplate).where(ReportTemplate.organization_id == organization_id)
        if type:
            q = q.where(ReportTemplate.type == type)
        if store_id is not None:
            q = q.where(ReportTemplate.store_id == store_id)
        if is_active is not None:
            q = q.where(ReportTemplate.is_active.is_(is_active))
        q = q.order_by(ReportTemplate.created_at.desc())
        result = await db.execute(q)
        return list(result.scalars().all())


report_repository: ReportRepository = ReportRepository()
report_template_repository: ReportTemplateRepository = ReportTemplateRepository()
