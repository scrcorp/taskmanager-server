"""체크리스트 인스턴스 레포지토리 — 체크리스트 인스턴스/아이템 DB 쿼리 담당.

Checklist Instance Repository — Handles all cl_instances and cl_instance_items
database queries. Extends BaseRepository with instance-specific filtering,
item completion management, and review operations.
"""

from datetime import date
from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.checklist import (
    ChecklistInstance,
    ChecklistInstanceItem,
    ChecklistItemFile,
    ChecklistItemMessage,
    ChecklistItemReviewLog,
    ChecklistItemSubmission,
)
from app.repositories.base import BaseRepository


class ChecklistInstanceRepository(BaseRepository[ChecklistInstance]):
    """체크리스트 인스턴스 레포지토리.

    Checklist instance repository with filtering, item management,
    and schedule-based lookups.

    Extends:
        BaseRepository[ChecklistInstance]
    """

    def __init__(self) -> None:
        super().__init__(ChecklistInstance)

    # ---------------------------------------------------------------------------
    # Instance queries
    # ---------------------------------------------------------------------------

    async def get_by_filters(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        user_id: UUID | None = None,
        work_date: date | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[ChecklistInstance], int]:
        """필터 조건에 맞는 체크리스트 인스턴스를 페이지네이션하여 조회합니다."""
        query: Select = (
            select(ChecklistInstance)
            .where(ChecklistInstance.organization_id == organization_id)
        )

        if store_id is not None:
            query = query.where(ChecklistInstance.store_id == store_id)
        if user_id is not None:
            query = query.where(ChecklistInstance.user_id == user_id)
        if work_date is not None:
            query = query.where(ChecklistInstance.work_date == work_date)
        if status is not None:
            query = query.where(ChecklistInstance.status == status)

        query = query.order_by(ChecklistInstance.work_date.desc(), ChecklistInstance.created_at.desc())

        return await self.get_paginated(db, query, page, per_page)

    async def get_with_items(
        self,
        db: AsyncSession,
        instance_id: UUID,
        organization_id: UUID | None = None,
    ) -> ChecklistInstance | None:
        """인스턴스를 아이템/파일/제출/리뷰로그/메시지와 함께 eager loading 조회합니다."""
        query: Select = (
            select(ChecklistInstance)
            .where(ChecklistInstance.id == instance_id)
            .options(
                selectinload(ChecklistInstance.items)
                .selectinload(ChecklistInstanceItem.files),
                selectinload(ChecklistInstance.items)
                .selectinload(ChecklistInstanceItem.submissions),
                selectinload(ChecklistInstance.items)
                .selectinload(ChecklistInstanceItem.reviews_log),
                selectinload(ChecklistInstance.items)
                .selectinload(ChecklistInstanceItem.messages),
            )
        )

        if organization_id is not None:
            query = query.where(ChecklistInstance.organization_id == organization_id)

        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_by_schedule_id(
        self,
        db: AsyncSession,
        schedule_id: UUID,
    ) -> ChecklistInstance | None:
        """스케줄 ID로 인스턴스를 아이템과 함께 조회합니다."""
        query: Select = (
            select(ChecklistInstance)
            .where(ChecklistInstance.schedule_id == schedule_id)
            .options(
                selectinload(ChecklistInstance.items)
                .selectinload(ChecklistInstanceItem.files),
                selectinload(ChecklistInstance.items)
                .selectinload(ChecklistInstanceItem.submissions),
                selectinload(ChecklistInstance.items)
                .selectinload(ChecklistInstanceItem.reviews_log),
                selectinload(ChecklistInstance.items)
                .selectinload(ChecklistInstanceItem.messages),
            )
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_user_instances(
        self,
        db: AsyncSession,
        user_id: UUID,
        work_date: date | None = None,
    ) -> Sequence[ChecklistInstance]:
        """특정 사용자의 체크리스트 인스턴스 목록을 조회합니다 (앱용)."""
        query: Select = (
            select(ChecklistInstance)
            .where(ChecklistInstance.user_id == user_id)
        )

        if work_date is not None:
            query = query.where(ChecklistInstance.work_date == work_date)

        query = query.order_by(ChecklistInstance.work_date.desc(), ChecklistInstance.created_at.desc())
        result = await db.execute(query)
        return result.scalars().all()

    # ---------------------------------------------------------------------------
    # Instance item queries
    # ---------------------------------------------------------------------------

    async def get_item(
        self,
        db: AsyncSession,
        instance_id: UUID,
        item_index: int,
    ) -> ChecklistInstanceItem | None:
        """인스턴스의 특정 항목을 조회합니다."""
        query: Select = (
            select(ChecklistInstanceItem)
            .where(
                ChecklistInstanceItem.instance_id == instance_id,
                ChecklistInstanceItem.item_index == item_index,
            )
            .options(
                selectinload(ChecklistInstanceItem.files),
                selectinload(ChecklistInstanceItem.submissions),
                selectinload(ChecklistInstanceItem.reviews_log),
                selectinload(ChecklistInstanceItem.messages),
            )
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def create_item(
        self,
        db: AsyncSession,
        item_data: dict,
    ) -> ChecklistInstanceItem:
        """체크리스트 인스턴스 항목을 생성합니다."""
        item = ChecklistInstanceItem(**item_data)
        db.add(item)
        await db.flush()
        await db.refresh(item)
        return item

    # ---------------------------------------------------------------------------
    # Review summary (queries cl_instance_items for review_result)
    # ---------------------------------------------------------------------------

    async def get_review_summary(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> dict:
        """리뷰 요약 통계를 집계합니다."""
        filters = [ChecklistInstance.organization_id == organization_id]
        if store_id is not None:
            filters.append(ChecklistInstance.store_id == store_id)
        if date_from is not None:
            filters.append(ChecklistInstance.work_date >= date_from)
        if date_to is not None:
            filters.append(ChecklistInstance.work_date <= date_to)

        # total_items, completed_items from cl_instances
        totals_q = select(
            func.coalesce(func.sum(ChecklistInstance.total_items), 0).label("total_items"),
            func.coalesce(func.sum(ChecklistInstance.completed_items), 0).label("completed_items"),
        ).where(*filters)

        totals_result = await db.execute(totals_q)
        totals_row = totals_result.one()

        # review counts from cl_instance_items
        review_q = (
            select(
                ChecklistInstanceItem.review_result,
                func.count().label("cnt"),
            )
            .join(ChecklistInstance, ChecklistInstanceItem.instance_id == ChecklistInstance.id)
            .where(*filters, ChecklistInstanceItem.review_result.isnot(None))
            .group_by(ChecklistInstanceItem.review_result)
        )

        review_result = await db.execute(review_q)
        review_counts: dict[str, int] = {row.review_result: row.cnt for row in review_result.all()}

        reviewed_items = sum(review_counts.values())
        total_items_val = int(totals_row.total_items)

        # total assignments
        total_assignments_q = select(func.count()).select_from(ChecklistInstance).where(*filters)
        total_assignments_result = await db.execute(total_assignments_q)
        total_assignments = total_assignments_result.scalar_one()

        # fully approved: all items have review_result = 'pass'
        pass_counts = (
            select(
                ChecklistInstanceItem.instance_id,
                func.count().label("pass_cnt"),
            )
            .join(ChecklistInstance, ChecklistInstanceItem.instance_id == ChecklistInstance.id)
            .where(*filters, ChecklistInstanceItem.review_result == "pass")
            .group_by(ChecklistInstanceItem.instance_id)
        ).subquery()

        fully_approved_q = (
            select(func.count())
            .select_from(ChecklistInstance)
            .join(pass_counts, ChecklistInstance.id == pass_counts.c.instance_id)
            .where(
                *filters,
                ChecklistInstance.total_items > 0,
                pass_counts.c.pass_cnt == ChecklistInstance.total_items,
            )
        )
        fully_approved_result = await db.execute(fully_approved_q)
        fully_approved = fully_approved_result.scalar_one()

        return {
            "total_items": total_items_val,
            "completed_items": int(totals_row.completed_items),
            "reviewed_items": reviewed_items,
            "pass": review_counts.get("pass", 0),
            "fail": review_counts.get("fail", 0),
            "caution": review_counts.get("caution", 0),
            "pending_re_review": review_counts.get("pending_re_review", 0),
            "unreviewed": total_items_val - reviewed_items,
            "total_assignments": total_assignments,
            "fully_approved_assignments": fully_approved,
        }


# 싱글턴 인스턴스 — Singleton instance
checklist_instance_repository: ChecklistInstanceRepository = ChecklistInstanceRepository()
