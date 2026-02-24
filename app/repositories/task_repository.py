"""추가 업무 레포지토리 — 추가 업무 관련 DB 쿼리 담당.

Additional Task Repository — Handles all additional-task-related database queries.
Extends BaseRepository with task-specific filtering and assignee management.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.communication import AdditionalTask, AdditionalTaskAssignee
from app.repositories.base import BaseRepository


class TaskRepository(BaseRepository[AdditionalTask]):
    """추가 업무 레포지토리.

    Additional task repository with assignee management and filtering.

    Extends:
        BaseRepository[AdditionalTask]
    """

    def __init__(self) -> None:
        """레포지토리를 초기화합니다.

        Initialize the task repository with AdditionalTask model.
        """
        super().__init__(AdditionalTask)

    async def get_by_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
        filters: dict | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[AdditionalTask], int]:
        """조직별 추가 업무를 필터링하여 페이지네이션 조회합니다.

        Retrieve paginated additional tasks for an org with optional filters.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            filters: 추가 필터 (store_id, status, priority)
                     (Additional filters: store_id, status, priority)
            page: 페이지 번호, 1부터 시작 (Page number, 1-based)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[AdditionalTask], int]: (업무 목록, 전체 개수)
                                                   (List of tasks, total count)
        """
        query: Select = (
            select(AdditionalTask)
            .where(AdditionalTask.organization_id == organization_id)
            .options(selectinload(AdditionalTask.assignees))
        )

        if filters:
            if filters.get("store_id") is not None:
                query = query.where(AdditionalTask.store_id == filters["store_id"])
            elif filters.get("store_ids") is not None:
                query = query.where(AdditionalTask.store_id.in_(filters["store_ids"]))
            if filters.get("status") is not None:
                query = query.where(AdditionalTask.status == filters["status"])
            if filters.get("priority") is not None:
                query = query.where(AdditionalTask.priority == filters["priority"])

        query = query.order_by(AdditionalTask.created_at.desc())
        return await self.get_paginated(db, query, page, per_page)

    async def get_detail_with_assignees(
        self,
        db: AsyncSession,
        task_id: UUID,
        organization_id: UUID,
    ) -> AdditionalTask | None:
        """업무 상세를 담당자 목록과 함께 조회합니다.

        Retrieve task detail with assignees eagerly loaded.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            task_id: 업무 UUID (Task UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            AdditionalTask | None: 업무 상세 또는 None (Task detail or None)
        """
        query: Select = (
            select(AdditionalTask)
            .where(
                AdditionalTask.id == task_id,
                AdditionalTask.organization_id == organization_id,
            )
            .options(selectinload(AdditionalTask.assignees))
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_user_tasks(
        self,
        db: AsyncSession,
        user_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[AdditionalTask], int]:
        """사용자에게 배정된 추가 업무를 조회합니다.

        Retrieve additional tasks assigned to a specific user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)
            page: 페이지 번호, 1부터 시작 (Page number, 1-based)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[AdditionalTask], int]: (업무 목록, 전체 개수)
                                                   (List of tasks, total count)
        """
        query: Select = (
            select(AdditionalTask)
            .join(AdditionalTaskAssignee)
            .where(AdditionalTaskAssignee.user_id == user_id)
            .options(selectinload(AdditionalTask.assignees))
            .order_by(AdditionalTask.created_at.desc())
        )
        return await self.get_paginated(db, query, page, per_page)

    async def add_assignees(
        self,
        db: AsyncSession,
        task_id: UUID,
        user_ids: list[UUID],
    ) -> None:
        """업무에 담당자를 추가합니다.

        Add assignees to an additional task.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            task_id: 업무 UUID (Task UUID)
            user_ids: 추가할 사용자 UUID 목록 (List of user UUIDs to add)
        """
        for uid in user_ids:
            assignee: AdditionalTaskAssignee = AdditionalTaskAssignee(
                task_id=task_id,
                user_id=uid,
            )
            db.add(assignee)
        await db.flush()

    async def get_assignee(
        self,
        db: AsyncSession,
        task_id: UUID,
        user_id: UUID,
    ) -> AdditionalTaskAssignee | None:
        """특정 업무의 담당자 정보를 조회합니다.

        Retrieve a specific assignee record for a task.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            task_id: 업무 UUID (Task UUID)
            user_id: 사용자 UUID (User UUID)

        Returns:
            AdditionalTaskAssignee | None: 담당자 정보 또는 None (Assignee record or None)
        """
        query: Select = select(AdditionalTaskAssignee).where(
            AdditionalTaskAssignee.task_id == task_id,
            AdditionalTaskAssignee.user_id == user_id,
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def remove_assignees(
        self,
        db: AsyncSession,
        task_id: UUID,
    ) -> None:
        """업무의 모든 담당자를 제거합니다.

        Remove all assignees from a task.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            task_id: 업무 UUID (Task UUID)
        """
        from sqlalchemy import delete as sa_delete

        await db.execute(
            sa_delete(AdditionalTaskAssignee).where(
                AdditionalTaskAssignee.task_id == task_id
            )
        )
        await db.flush()


# 싱글턴 인스턴스 — Singleton instance
task_repository: TaskRepository = TaskRepository()
