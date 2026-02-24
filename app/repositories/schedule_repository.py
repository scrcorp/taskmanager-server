"""스케줄 레포지토리 — 스케줄 관련 DB 쿼리 담당.

Schedule Repository — Handles all schedule-related database queries.
Extends BaseRepository with schedule-specific filtering, duplicate checks,
and approval record creation.
"""

from datetime import date
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import Schedule, ScheduleApproval
from app.repositories.base import BaseRepository


class ScheduleRepository(BaseRepository[Schedule]):
    """스케줄 레포지토리.

    Schedule repository with filtering, duplicate checks, and approval management.

    Extends:
        BaseRepository[Schedule]
    """

    def __init__(self) -> None:
        """레포지토리를 초기화합니다.

        Initialize the schedule repository with Schedule model.
        """
        super().__init__(Schedule)

    async def get_by_filters(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        user_id: UUID | None = None,
        work_date: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Schedule], int]:
        """필터 조건에 맞는 스케줄을 페이지네이션하여 조회합니다.

        Retrieve paginated schedules matching the given filters.
        date_from/date_to 범위 필터가 있으면 work_date보다 우선합니다.
        (Date range filters take precedence over single work_date.)

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            store_id: 매장 UUID 필터, 선택 (Optional store UUID filter)
            user_id: 사용자 UUID 필터, 선택 (Optional user UUID filter)
            work_date: 근무일 필터, 선택 (Optional work date filter)
            date_from: 시작일 범위 필터, 선택 (Optional range start date)
            date_to: 종료일 범위 필터, 선택 (Optional range end date)
            status: 상태 필터, 선택 (Optional status filter)
            page: 페이지 번호, 1부터 시작 (Page number, 1-based)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[Schedule], int]: (스케줄 목록, 전체 개수)
                                             (List of schedules, total count)
        """
        query: Select = (
            select(Schedule)
            .where(Schedule.organization_id == organization_id)
        )

        if store_id is not None:
            query = query.where(Schedule.store_id == store_id)
        if user_id is not None:
            query = query.where(Schedule.user_id == user_id)
        if date_from is not None or date_to is not None:
            if date_from is not None:
                query = query.where(Schedule.work_date >= date_from)
            if date_to is not None:
                query = query.where(Schedule.work_date <= date_to)
        elif work_date is not None:
            query = query.where(Schedule.work_date == work_date)
        if status is not None:
            query = query.where(Schedule.status == status)

        query = query.order_by(Schedule.work_date.desc(), Schedule.created_at.desc())

        return await self.get_paginated(db, query, page, per_page)

    async def get_by_id_with_org(
        self,
        db: AsyncSession,
        schedule_id: UUID,
        organization_id: UUID,
    ) -> Schedule | None:
        """스케줄 상세 정보를 조직 범위로 조회합니다.

        Retrieve a single schedule scoped by organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            schedule_id: 스케줄 UUID (Schedule UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            Schedule | None: 스케줄 또는 None (Schedule or None)
        """
        return await self.get_by_id(db, schedule_id, organization_id)

    async def get_pending_count(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> int:
        """승인 대기 중인 스케줄 수를 조회합니다 (GM 대시보드용).

        Count pending schedules for the GM dashboard.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            int: 승인 대기 스케줄 수 (Count of pending schedules)
        """
        result = await db.execute(
            select(func.count())
            .select_from(Schedule)
            .where(
                Schedule.organization_id == organization_id,
                Schedule.status == "pending",
            )
        )
        return result.scalar() or 0

    async def check_duplicate(
        self,
        db: AsyncSession,
        user_id: UUID,
        store_id: UUID,
        work_date: date,
        shift_id: UUID | None,
        exclude_id: UUID | None = None,
    ) -> bool:
        """동일 사용자+매장+날짜+시프트 조합의 중복 스케줄이 있는지 확인합니다.

        Check if a duplicate schedule exists for the same user+store+date+shift combo.
        Excludes cancelled schedules from duplicate checks.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)
            store_id: 매장 UUID (Store UUID)
            work_date: 근무일 (Work date)
            shift_id: 시간대 UUID, nullable (Shift UUID)
            exclude_id: 제외할 스케줄 UUID, 선택 (Schedule UUID to exclude — for updates)

        Returns:
            bool: 중복 존재 여부 (Whether a duplicate exists)
        """
        query = (
            select(func.count())
            .select_from(Schedule)
            .where(
                Schedule.user_id == user_id,
                Schedule.store_id == store_id,
                Schedule.work_date == work_date,
                Schedule.status != "cancelled",
            )
        )

        # shift_id가 None인 경우와 아닌 경우 분리 처리
        # Handle NULL shift_id comparison correctly
        if shift_id is not None:
            query = query.where(Schedule.shift_id == shift_id)
        else:
            query = query.where(Schedule.shift_id.is_(None))

        if exclude_id is not None:
            query = query.where(Schedule.id != exclude_id)

        count: int = (await db.execute(query)).scalar() or 0
        return count > 0

    async def create_approval(
        self,
        db: AsyncSession,
        data: dict[str, Any],
    ) -> ScheduleApproval:
        """승인 이력 레코드를 생성합니다.

        Create an approval audit trail record.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            data: 승인 이력 데이터 딕셔너리 (Approval record data dict)

        Returns:
            ScheduleApproval: 생성된 승인 이력 (Created approval record)
        """
        approval: ScheduleApproval = ScheduleApproval(**data)
        db.add(approval)
        await db.flush()
        await db.refresh(approval)
        return approval


# 싱글턴 인스턴스 — Singleton instance
schedule_repository: ScheduleRepository = ScheduleRepository()
