"""업무 배정 레포지토리 — 업무 배정 관련 DB 쿼리 담당.

Work Assignment Repository — Handles all assignment-related database queries.
Extends BaseRepository with assignment-specific filtering and user queries.
"""

from datetime import date
from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assignment import WorkAssignment
from app.repositories.base import BaseRepository


class AssignmentRepository(BaseRepository[WorkAssignment]):
    """업무 배정 레포지토리.

    Work assignment repository with filtering and user-specific queries.

    Extends:
        BaseRepository[WorkAssignment]
    """

    def __init__(self) -> None:
        """레포지토리를 초기화합니다.

        Initialize the assignment repository with WorkAssignment model.
        """
        super().__init__(WorkAssignment)

    async def get_by_filters(
        self,
        db: AsyncSession,
        organization_id: UUID,
        brand_id: UUID | None = None,
        user_id: UUID | None = None,
        work_date: date | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[WorkAssignment], int]:
        """필터 조건에 맞는 업무 배정을 페이지네이션하여 조회합니다.

        Retrieve paginated work assignments matching the given filters.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            brand_id: 브랜드 UUID 필터, 선택 (Optional brand UUID filter)
            user_id: 사용자 UUID 필터, 선택 (Optional user UUID filter)
            work_date: 근무일 필터, 선택 (Optional work date filter)
            status: 상태 필터, 선택 (Optional status filter)
            page: 페이지 번호, 1부터 시작 (Page number, 1-based)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[WorkAssignment], int]: (배정 목록, 전체 개수)
                                                   (List of assignments, total count)
        """
        query: Select = (
            select(WorkAssignment)
            .where(WorkAssignment.organization_id == organization_id)
        )

        if brand_id is not None:
            query = query.where(WorkAssignment.brand_id == brand_id)
        if user_id is not None:
            query = query.where(WorkAssignment.user_id == user_id)
        if work_date is not None:
            query = query.where(WorkAssignment.work_date == work_date)
        if status is not None:
            query = query.where(WorkAssignment.status == status)

        query = query.order_by(WorkAssignment.work_date.desc(), WorkAssignment.created_at.desc())

        return await self.get_paginated(db, query, page, per_page)

    async def get_detail(
        self,
        db: AsyncSession,
        assignment_id: UUID,
        organization_id: UUID,
    ) -> WorkAssignment | None:
        """업무 배정 상세 정보를 조회합니다.

        Retrieve detailed work assignment information.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            assignment_id: 배정 UUID (Assignment UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            WorkAssignment | None: 배정 상세 또는 None (Assignment detail or None)
        """
        return await self.get_by_id(db, assignment_id, organization_id)

    async def get_user_assignments(
        self,
        db: AsyncSession,
        user_id: UUID,
        work_date: date | None = None,
        status: str | None = None,
    ) -> Sequence[WorkAssignment]:
        """특정 사용자의 업무 배정 목록을 조회합니다.

        Retrieve work assignments for a specific user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)
            work_date: 근무일 필터, 선택 (Optional work date filter)
            status: 상태 필터, 선택 (Optional status filter)

        Returns:
            Sequence[WorkAssignment]: 사용자의 배정 목록 (User's assignment list)
        """
        query: Select = (
            select(WorkAssignment)
            .where(WorkAssignment.user_id == user_id)
        )

        if work_date is not None:
            query = query.where(WorkAssignment.work_date == work_date)
        if status is not None:
            query = query.where(WorkAssignment.status == status)

        query = query.order_by(WorkAssignment.work_date.desc(), WorkAssignment.created_at.desc())
        result = await db.execute(query)
        return result.scalars().all()

    async def get_recent_user_ids(
        self,
        db: AsyncSession,
        organization_id: UUID,
        brand_id: UUID,
        exclude_date: date | None = None,
        days: int = 30,
    ) -> Sequence[tuple]:
        """브랜드 내 최근 배정된 사용자 ID를 shift×position 조합별로 조회합니다.

        Retrieve recently assigned user IDs grouped by shift×position combo.
        Returns (shift_id, position_id, user_id, last_work_date) tuples
        ordered by most recent first.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            brand_id: 브랜드 UUID (Brand UUID)
            exclude_date: 제외할 날짜, 보통 오늘 (Date to exclude, usually today)
            days: 조회 기간 일수, 기본 30일 (Lookback period in days, default 30)

        Returns:
            Sequence[tuple]: (shift_id, position_id, user_id, last_work_date) 목록
        """
        from datetime import timedelta

        cutoff: date = date.today() - timedelta(days=days)

        query = (
            select(
                WorkAssignment.shift_id,
                WorkAssignment.position_id,
                WorkAssignment.user_id,
                func.max(WorkAssignment.work_date).label("last_work_date"),
            )
            .where(
                WorkAssignment.organization_id == organization_id,
                WorkAssignment.brand_id == brand_id,
                WorkAssignment.work_date >= cutoff,
            )
            .group_by(
                WorkAssignment.shift_id,
                WorkAssignment.position_id,
                WorkAssignment.user_id,
            )
            .order_by(func.max(WorkAssignment.work_date).desc())
        )

        if exclude_date is not None:
            query = query.having(func.max(WorkAssignment.work_date) != exclude_date)

        result = await db.execute(query)
        return result.all()

    async def check_duplicate(
        self,
        db: AsyncSession,
        brand_id: UUID,
        shift_id: UUID,
        position_id: UUID,
        user_id: UUID,
        work_date: date,
    ) -> bool:
        """같은 날짜에 동일 조합의 중복 배정이 있는지 확인합니다.

        Check if a duplicate assignment exists for the same combination on the same date.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            brand_id: 브랜드 UUID (Brand UUID)
            shift_id: 근무조 UUID (Shift UUID)
            position_id: 포지션 UUID (Position UUID)
            user_id: 사용자 UUID (User UUID)
            work_date: 근무일 (Work date)

        Returns:
            bool: 중복 존재 여부 (Whether a duplicate exists)
        """
        count: int = (
            await db.execute(
                select(func.count())
                .select_from(WorkAssignment)
                .where(
                    WorkAssignment.brand_id == brand_id,
                    WorkAssignment.shift_id == shift_id,
                    WorkAssignment.position_id == position_id,
                    WorkAssignment.user_id == user_id,
                    WorkAssignment.work_date == work_date,
                )
            )
        ).scalar() or 0
        return count > 0


# 싱글턴 인스턴스 — Singleton instance
assignment_repository: AssignmentRepository = AssignmentRepository()
