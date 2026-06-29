"""Repository for unified multi-type Report."""
from datetime import date
from typing import Sequence
from uuid import UUID

from sqlalchemy import Date, Select, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.report import Report, ReportTemplate, ReportType
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
            .options(
                selectinload(Report.comments),
                selectinload(Report.acknowledgements),
            )
            .where(
                Report.id == report_id,
                Report.organization_id == organization_id,
                Report.deleted_at.is_(None),
            )
            # expire_on_commit=False 환경: 같은 세션에서 commit 후 재조회 시
            # identity-map 의 이미 로드된 comments/acknowledgements 컬렉션이 갱신되지
            # 않는다. populate_existing 으로 강제 refresh (detail 응답 정확성 보장).
            .execution_options(populate_existing=True)
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
            base.options(
                selectinload(Report.comments),
                selectinload(Report.acknowledgements),
            )
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
        author_id: UUID | None = None,
    ) -> Report | None:
        """daily 중복 탐지. per-person 유일성(결정-8): author_id 포함 시 같은
        작성자의 (store, date, period) 중복만 매칭. author_id 미지정(레거시 호출)
        시에는 store/date/period 전역 중복을 본다."""
        conditions = [
            Report.type == "daily",
            Report.store_id == store_id,
            Report.report_date == report_date,
            Report.payload["period"].astext == period,
            Report.deleted_at.is_(None),
        ]
        if author_id is not None:
            conditions.append(Report.author_id == author_id)
        result = await db.execute(select(Report).where(*conditions))
        return result.scalar_one_or_none()


class ReportTemplateRepository:
    async def get_by_id(
        self, db: AsyncSession, template_id: UUID
    ) -> ReportTemplate | None:
        result = await db.execute(select(ReportTemplate).where(ReportTemplate.id == template_id))
        return result.scalar_one_or_none()

    @staticmethod
    def _pick_for_type_code(
        templates: list[ReportTemplate], type_code: str | None
    ) -> ReportTemplate | None:
        """한 scope 안의 후보 템플릿들 중 type_code 에 맞는 것을 고른다.

        결정-9: applicable_types 에 type_code 가 포함된 템플릿을 우선,
        없으면 applicable_types 가 null/[] 인 전체(all-types) 템플릿으로 fallback.
        type_code 미지정이면 기존 동작(첫 활성 템플릿)을 유지.
        """
        if not templates:
            return None
        if type_code is None:
            return templates[0]
        # 1) 정확히 이 type_code 를 명시한 템플릿
        for t in templates:
            at = t.applicable_types or []
            if type_code in at:
                return t
        # 2) 전체 적용(all-types) 템플릿
        for t in templates:
            if not t.applicable_types:
                return t
        return None

    async def get_template_for_store(
        self,
        db: AsyncSession,
        type: str,
        organization_id: UUID,
        store_id: UUID | None = None,
        type_code: str | None = None,
    ) -> ReportTemplate | None:
        # 1. Store-specific
        if store_id:
            result = await db.execute(
                select(ReportTemplate)
                .where(
                    ReportTemplate.type == type,
                    ReportTemplate.store_id == store_id,
                    ReportTemplate.is_active.is_(True),
                )
                .order_by(ReportTemplate.created_at.desc())
            )
            t = self._pick_for_type_code(list(result.scalars().all()), type_code)
            if t:
                return t

        # 2. Org-level
        result = await db.execute(
            select(ReportTemplate)
            .where(
                ReportTemplate.type == type,
                ReportTemplate.organization_id == organization_id,
                ReportTemplate.store_id.is_(None),
                ReportTemplate.is_active.is_(True),
            )
            .order_by(ReportTemplate.created_at.desc())
        )
        t = self._pick_for_type_code(list(result.scalars().all()), type_code)
        if t:
            return t

        # 3. System default
        result = await db.execute(
            select(ReportTemplate)
            .where(
                ReportTemplate.type == type,
                ReportTemplate.organization_id.is_(None),
                ReportTemplate.is_default.is_(True),
                ReportTemplate.is_active.is_(True),
            )
            .order_by(ReportTemplate.created_at.desc())
        )
        return self._pick_for_type_code(list(result.scalars().all()), type_code)

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


class ReportTypeRepository:
    """report_types CRUD + resolution 데이터 액세스. (순수 쿼리)"""

    async def get_by_id(
        self, db: AsyncSession, type_id: UUID, organization_id: UUID
    ) -> ReportType | None:
        result = await db.execute(
            select(ReportType).where(
                ReportType.id == type_id,
                ReportType.organization_id == organization_id,
                ReportType.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def list_org_defaults(
        self, db: AsyncSession, organization_id: UUID
    ) -> list[ReportType]:
        result = await db.execute(
            select(ReportType)
            .where(
                ReportType.organization_id == organization_id,
                ReportType.store_id.is_(None),
                ReportType.is_deleted.is_(False),
            )
            .order_by(ReportType.sort_order, ReportType.label)
        )
        return list(result.scalars().all())

    async def list_store_rows(
        self, db: AsyncSession, organization_id: UUID, store_id: UUID
    ) -> list[ReportType]:
        result = await db.execute(
            select(ReportType)
            .where(
                ReportType.organization_id == organization_id,
                ReportType.store_id == store_id,
                ReportType.is_deleted.is_(False),
            )
            .order_by(ReportType.sort_order, ReportType.label)
        )
        return list(result.scalars().all())

    async def list_for_scope(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None,
    ) -> list[ReportType]:
        """관리용 raw 목록 (resolve 안 함). store_id None → org-default 행만."""
        q = select(ReportType).where(
            ReportType.organization_id == organization_id,
            ReportType.is_deleted.is_(False),
        )
        if store_id is None:
            q = q.where(ReportType.store_id.is_(None))
        else:
            q = q.where(ReportType.store_id == store_id)
        q = q.order_by(ReportType.sort_order, ReportType.label)
        result = await db.execute(q)
        return list(result.scalars().all())

    async def find_live_by_code(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None,
        code: str,
    ) -> ReportType | None:
        q = select(ReportType).where(
            ReportType.organization_id == organization_id,
            ReportType.code == code,
            ReportType.is_deleted.is_(False),
        )
        if store_id is None:
            q = q.where(ReportType.store_id.is_(None))
        else:
            q = q.where(ReportType.store_id == store_id)
        result = await db.execute(q)
        return result.scalar_one_or_none()


report_repository: ReportRepository = ReportRepository()
report_template_repository: ReportTemplateRepository = ReportTemplateRepository()
report_type_repository: ReportTypeRepository = ReportTypeRepository()
