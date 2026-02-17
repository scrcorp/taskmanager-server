"""추가 업무 서비스 — 추가 업무 비즈니스 로직.

Task Service — Business logic for additional task management.
Handles admin CRUD, assignee management, and app-facing completion tracking.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import AdditionalTask, AdditionalTaskAssignee
from app.models.organization import Brand
from app.models.user import User
from app.repositories.task_repository import task_repository
from app.schemas.common import TaskCreate, TaskUpdate
from app.utils.exceptions import BadRequestError, ForbiddenError, NotFoundError


class TaskService:
    """추가 업무 서비스.

    Additional task service providing admin CRUD, assignee management,
    and app-facing completion tracking.
    """

    async def _validate_brand_ownership(
        self,
        db: AsyncSession,
        brand_id: UUID,
        organization_id: UUID,
    ) -> Brand:
        """브랜드가 해당 조직에 속하는지 검증합니다.

        Verify that a brand belongs to the specified organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            brand_id: 브랜드 UUID (Brand UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            Brand: 검증된 브랜드 (Verified brand)

        Raises:
            NotFoundError: 브랜드가 없을 때 (When brand not found)
            ForbiddenError: 다른 조직 브랜드일 때 (When brand belongs to another org)
        """
        result = await db.execute(select(Brand).where(Brand.id == brand_id))
        brand: Brand | None = result.scalar_one_or_none()

        if brand is None:
            raise NotFoundError("브랜드를 찾을 수 없습니다 (Brand not found)")
        if brand.organization_id != organization_id:
            raise ForbiddenError("해당 브랜드에 대한 권한이 없습니다 (No permission for this brand)")
        return brand

    async def build_response(
        self,
        db: AsyncSession,
        task: AdditionalTask,
    ) -> dict:
        """추가 업무 응답 딕셔너리를 구성합니다 (관련 엔티티 이름 포함).

        Build additional task response dict with related entity names.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            task: 추가 업무 ORM 객체 (Additional task ORM object)

        Returns:
            dict: 브랜드명/작성자명/담당자명이 포함된 응답 딕셔너리
                  (Response dict with brand, creator, and assignee names)
        """
        # 브랜드 이름 조회 — Fetch brand name
        brand_name: str | None = None
        if task.brand_id is not None:
            result = await db.execute(select(Brand.name).where(Brand.id == task.brand_id))
            brand_name = result.scalar()

        # 작성자 이름 조회 — Fetch creator name
        creator_result = await db.execute(
            select(User.full_name).where(User.id == task.created_by)
        )
        created_by_name: str = creator_result.scalar() or "Unknown"

        # 담당자 이름 목록 조회 — Fetch assignee names
        assignee_names: list[str] = []
        if hasattr(task, "assignees") and task.assignees:
            for assignee in task.assignees:
                name_result = await db.execute(
                    select(User.full_name).where(User.id == assignee.user_id)
                )
                name: str | None = name_result.scalar()
                if name:
                    assignee_names.append(name)

        return {
            "id": str(task.id),
            "title": task.title,
            "description": task.description,
            "brand_id": str(task.brand_id) if task.brand_id else None,
            "brand_name": brand_name,
            "priority": task.priority,
            "status": task.status,
            "due_date": task.due_date,
            "created_by_name": created_by_name,
            "assignee_names": assignee_names,
            "created_at": task.created_at,
        }

    # --- Admin CRUD ---

    async def list_tasks(
        self,
        db: AsyncSession,
        organization_id: UUID,
        brand_id: UUID | None = None,
        status: str | None = None,
        priority: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[AdditionalTask], int]:
        """조직의 추가 업무 목록을 필터링하여 조회합니다.

        List additional tasks for an org with optional filters.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            brand_id: 브랜드 UUID 필터, 선택 (Optional brand UUID filter)
            status: 상태 필터, 선택 (Optional status filter)
            priority: 우선순위 필터, 선택 (Optional priority filter)
            page: 페이지 번호 (Page number)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[AdditionalTask], int]: (업무 목록, 전체 개수)
                                                   (List of tasks, total count)
        """
        filters: dict = {}
        if brand_id is not None:
            filters["brand_id"] = brand_id
        if status is not None:
            filters["status"] = status
        if priority is not None:
            filters["priority"] = priority

        return await task_repository.get_by_org(
            db, organization_id, filters, page, per_page
        )

    async def get_detail(
        self,
        db: AsyncSession,
        task_id: UUID,
        organization_id: UUID,
    ) -> AdditionalTask:
        """추가 업무 상세를 담당자 목록과 함께 조회합니다.

        Get additional task detail with assignees.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            task_id: 업무 UUID (Task UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            AdditionalTask: 업무 상세 (Task detail)

        Raises:
            NotFoundError: 업무가 없을 때 (When task not found)
        """
        task: AdditionalTask | None = await task_repository.get_detail_with_assignees(
            db, task_id, organization_id
        )
        if task is None:
            raise NotFoundError("추가 업무를 찾을 수 없습니다 (Additional task not found)")
        return task

    async def create_task(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: TaskCreate,
        created_by: UUID,
    ) -> AdditionalTask:
        """새 추가 업무를 생성하고 담당자를 배정합니다.

        Create a new additional task and assign users.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            data: 업무 생성 데이터 (Task creation data)
            created_by: 작성자 UUID (Creator's UUID)

        Returns:
            AdditionalTask: 생성된 업무 (Created task)

        Raises:
            NotFoundError: 브랜드가 없을 때 (When brand not found)
            ForbiddenError: 다른 조직 브랜드일 때 (When brand belongs to another org)
        """
        brand_id: UUID | None = UUID(data.brand_id) if data.brand_id else None

        if brand_id is not None:
            await self._validate_brand_ownership(db, brand_id, organization_id)

        task: AdditionalTask = await task_repository.create(
            db,
            {
                "organization_id": organization_id,
                "brand_id": brand_id,
                "title": data.title,
                "description": data.description,
                "priority": data.priority,
                "due_date": data.due_date,
                "created_by": created_by,
            },
        )

        # 담당자 배정 — Assign users
        assignee_uuids: list[UUID] = [UUID(uid) for uid in data.assignee_ids]
        if assignee_uuids:
            await task_repository.add_assignees(db, task.id, assignee_uuids)

            # 알림 자동 생성 — Auto-create notifications
            from app.services.notification_service import notification_service

            await notification_service.create_for_task(db, task, assignee_uuids)

        return task

    async def update_task(
        self,
        db: AsyncSession,
        task_id: UUID,
        organization_id: UUID,
        data: TaskUpdate,
    ) -> AdditionalTask:
        """추가 업무를 업데이트합니다.

        Update an additional task.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            task_id: 업무 UUID (Task UUID)
            organization_id: 조직 UUID (Organization UUID)
            data: 업데이트 데이터 (Update data)

        Returns:
            AdditionalTask: 업데이트된 업무 (Updated task)

        Raises:
            NotFoundError: 업무가 없을 때 (When task not found)
        """
        update_data: dict = data.model_dump(exclude_unset=True)
        updated: AdditionalTask | None = await task_repository.update(
            db, task_id, update_data, organization_id
        )
        if updated is None:
            raise NotFoundError("추가 업무를 찾을 수 없습니다 (Additional task not found)")
        return updated

    async def delete_task(
        self,
        db: AsyncSession,
        task_id: UUID,
        organization_id: UUID,
    ) -> bool:
        """추가 업무를 삭제합니다.

        Delete an additional task.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            task_id: 업무 UUID (Task UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            bool: 삭제 성공 여부 (Whether the deletion was successful)

        Raises:
            NotFoundError: 업무가 없을 때 (When task not found)
        """
        deleted: bool = await task_repository.delete(db, task_id, organization_id)
        if not deleted:
            raise NotFoundError("추가 업무를 찾을 수 없습니다 (Additional task not found)")
        return deleted

    # --- App (사용자용) ---

    async def list_my_tasks(
        self,
        db: AsyncSession,
        user_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[AdditionalTask], int]:
        """내게 배정된 추가 업무 목록을 조회합니다.

        List additional tasks assigned to the current user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)
            page: 페이지 번호 (Page number)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[AdditionalTask], int]: (업무 목록, 전체 개수)
                                                   (List of tasks, total count)
        """
        return await task_repository.get_user_tasks(db, user_id, page, per_page)

    async def complete_my_task(
        self,
        db: AsyncSession,
        task_id: UUID,
        user_id: UUID,
        organization_id: UUID,
    ) -> AdditionalTask:
        """내 추가 업무를 완료 처리합니다.

        Mark my additional task assignment as completed.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            task_id: 업무 UUID (Task UUID)
            user_id: 사용자 UUID (User UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            AdditionalTask: 업데이트된 업무 (Updated task)

        Raises:
            NotFoundError: 업무가 없거나 담당자가 아닐 때
                           (When task not found or user is not an assignee)
        """
        task: AdditionalTask | None = await task_repository.get_detail_with_assignees(
            db, task_id, organization_id
        )
        if task is None:
            raise NotFoundError("추가 업무를 찾을 수 없습니다 (Additional task not found)")

        # 담당자 확인 — Verify user is an assignee
        assignee: AdditionalTaskAssignee | None = await task_repository.get_assignee(
            db, task_id, user_id
        )
        if assignee is None:
            raise ForbiddenError(
                "이 업무의 담당자가 아닙니다 (You are not an assignee of this task)"
            )

        # 업무 상태를 completed로 변경 — Update task status to completed
        updated: AdditionalTask | None = await task_repository.update(
            db, task_id, {"status": "completed"}, organization_id
        )
        if updated is None:
            raise NotFoundError("추가 업무를 찾을 수 없습니다 (Additional task not found)")
        return updated


# 싱글턴 인스턴스 — Singleton instance
task_service: TaskService = TaskService()
