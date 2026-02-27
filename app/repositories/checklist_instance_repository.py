"""체크리스트 인스턴스 레포지토리 — 체크리스트 인스턴스/완료 기록 DB 쿼리 담당.

Checklist Instance Repository — Handles all cl_instances and cl_completions
database queries. Extends BaseRepository with instance-specific filtering,
completion management, and merged snapshot+completion views.
"""

from datetime import date
from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.checklist import ChecklistCompletion, ChecklistInstance, ChecklistItemReview
from app.repositories.base import BaseRepository


class ChecklistInstanceRepository(BaseRepository[ChecklistInstance]):
    """체크리스트 인스턴스 레포지토리.

    Checklist instance repository with filtering, completion management,
    and assignment-based lookups.

    Extends:
        BaseRepository[ChecklistInstance]
    """

    def __init__(self) -> None:
        """레포지토리를 초기화합니다.

        Initialize the checklist instance repository with ChecklistInstance model.
        """
        super().__init__(ChecklistInstance)

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
        """필터 조건에 맞는 체크리스트 인스턴스를 페이지네이션하여 조회합니다.

        Retrieve paginated checklist instances matching the given filters.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            store_id: 매장 UUID 필터, 선택 (Optional store UUID filter)
            user_id: 사용자 UUID 필터, 선택 (Optional user UUID filter)
            work_date: 근무일 필터, 선택 (Optional work date filter)
            status: 상태 필터, 선택 (Optional status filter)
            page: 페이지 번호, 1부터 시작 (Page number, 1-based)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[ChecklistInstance], int]: (인스턴스 목록, 전체 개수)
                                                      (List of instances, total count)
        """
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

    async def get_with_completions(
        self,
        db: AsyncSession,
        instance_id: UUID,
        organization_id: UUID | None = None,
    ) -> ChecklistInstance | None:
        """인스턴스를 완료 기록과 함께 조회합니다 (eager loading).

        Retrieve an instance with its completions eagerly loaded.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            instance_id: 인스턴스 UUID (Instance UUID)
            organization_id: 조직 UUID 필터, 선택 (Optional organization UUID filter)

        Returns:
            ChecklistInstance | None: 완료 기록 포함 인스턴스 또는 None
                                       (Instance with completions or None)
        """
        query: Select = (
            select(ChecklistInstance)
            .where(ChecklistInstance.id == instance_id)
            .options(
                selectinload(ChecklistInstance.completions),
                selectinload(ChecklistInstance.reviews),
            )
        )

        if organization_id is not None:
            query = query.where(ChecklistInstance.organization_id == organization_id)

        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_by_assignment_id(
        self,
        db: AsyncSession,
        work_assignment_id: UUID,
    ) -> ChecklistInstance | None:
        """근무 배정 ID로 인스턴스를 조회합니다.

        Retrieve a checklist instance by its work assignment ID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            work_assignment_id: 근무 배정 UUID (Work assignment UUID)

        Returns:
            ChecklistInstance | None: 인스턴스 또는 None (Instance or None)
        """
        query: Select = (
            select(ChecklistInstance)
            .where(ChecklistInstance.work_assignment_id == work_assignment_id)
            .options(
                selectinload(ChecklistInstance.completions),
                selectinload(ChecklistInstance.reviews),
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
        """특정 사용자의 체크리스트 인스턴스 목록을 조회합니다 (앱용).

        Retrieve checklist instances for a specific user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)
            work_date: 근무일 필터, 선택 (Optional work date filter)

        Returns:
            Sequence[ChecklistInstance]: 사용자의 인스턴스 목록 (User's instance list)
        """
        query: Select = (
            select(ChecklistInstance)
            .where(ChecklistInstance.user_id == user_id)
        )

        if work_date is not None:
            query = query.where(ChecklistInstance.work_date == work_date)

        query = query.order_by(ChecklistInstance.work_date.desc(), ChecklistInstance.created_at.desc())
        result = await db.execute(query)
        return result.scalars().all()

    async def create_completion(
        self,
        db: AsyncSession,
        completion_data: dict,
    ) -> ChecklistCompletion:
        """체크리스트 항목 완료 기록을 생성합니다.

        Create a checklist item completion record.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            completion_data: 완료 기록 데이터 딕셔너리 (Completion data dictionary)

        Returns:
            ChecklistCompletion: 생성된 완료 기록 (Created completion record)
        """
        completion: ChecklistCompletion = ChecklistCompletion(**completion_data)
        db.add(completion)
        await db.flush()
        await db.refresh(completion)
        return completion

    async def get_completion(
        self,
        db: AsyncSession,
        instance_id: UUID,
        item_index: int,
    ) -> ChecklistCompletion | None:
        """인스턴스의 특정 항목 완료 기록을 조회합니다.

        Retrieve a completion record for a specific item in an instance.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            instance_id: 인스턴스 UUID (Instance UUID)
            item_index: 항목 인덱스 (Item index)

        Returns:
            ChecklistCompletion | None: 완료 기록 또는 None (Completion or None)
        """
        query: Select = (
            select(ChecklistCompletion)
            .where(
                ChecklistCompletion.instance_id == instance_id,
                ChecklistCompletion.item_index == item_index,
            )
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def delete_completion(
        self,
        db: AsyncSession,
        completion: ChecklistCompletion,
    ) -> None:
        """체크리스트 항목 완료 기록을 삭제합니다 (완료 취소).

        Delete a checklist item completion record (uncomplete).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            completion: 삭제할 완료 기록 (Completion record to delete)
        """
        await db.delete(completion)
        await db.flush()


# 싱글턴 인스턴스 — Singleton instance
checklist_instance_repository: ChecklistInstanceRepository = ChecklistInstanceRepository()
