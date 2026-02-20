"""체크리스트 템플릿 레포지토리 — 체크리스트 관련 DB 쿼리 담당.

Checklist Template Repository — Handles all checklist-related database queries.
Extends BaseRepository with template-specific operations including item management.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.checklist import ChecklistTemplate, ChecklistTemplateItem
from app.models.organization import Store
from app.models.work import Position, Shift
from app.repositories.base import BaseRepository


class ChecklistRepository(BaseRepository[ChecklistTemplate]):
    """체크리스트 템플릿 레포지토리.

    Checklist template repository with item management operations.

    Extends:
        BaseRepository[ChecklistTemplate]
    """

    def __init__(self) -> None:
        """레포지토리를 초기화합니다.

        Initialize the checklist repository with ChecklistTemplate model.
        """
        super().__init__(ChecklistTemplate)

    async def get_all_by_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        shift_id: UUID | None = None,
        position_id: UUID | None = None,
    ) -> Sequence[ChecklistTemplate]:
        """조직 전체의 체크리스트 템플릿 목록을 조회합니다.

        Retrieve all checklist templates for an organization with optional filters.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            store_id: 매장 UUID 필터, 선택 (Optional store UUID filter)
            shift_id: 근무조 UUID 필터, 선택 (Optional shift UUID filter)
            position_id: 포지션 UUID 필터, 선택 (Optional position UUID filter)

        Returns:
            Sequence[ChecklistTemplate]: 템플릿 목록 (List of templates)
        """
        query: Select = (
            select(ChecklistTemplate)
            .join(Store, ChecklistTemplate.store_id == Store.id)
            .where(Store.organization_id == organization_id)
            .options(
                selectinload(ChecklistTemplate.items),
                selectinload(ChecklistTemplate.shift),
                selectinload(ChecklistTemplate.position),
            )
        )

        if store_id is not None:
            query = query.where(ChecklistTemplate.store_id == store_id)
        if shift_id is not None:
            query = query.where(ChecklistTemplate.shift_id == shift_id)
        if position_id is not None:
            query = query.where(ChecklistTemplate.position_id == position_id)

        query = query.order_by(ChecklistTemplate.created_at.desc())
        result = await db.execute(query)
        return result.scalars().all()

    async def get_by_store(
        self,
        db: AsyncSession,
        store_id: UUID,
        shift_id: UUID | None = None,
        position_id: UUID | None = None,
    ) -> Sequence[ChecklistTemplate]:
        """매장별 체크리스트 템플릿 목록을 조회합니다.

        Retrieve checklist templates filtered by store and optional shift/position.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 UUID (Store UUID)
            shift_id: 근무조 UUID 필터, 선택 (Optional shift UUID filter)
            position_id: 포지션 UUID 필터, 선택 (Optional position UUID filter)

        Returns:
            Sequence[ChecklistTemplate]: 템플릿 목록 (List of templates)
        """
        query: Select = (
            select(ChecklistTemplate)
            .where(ChecklistTemplate.store_id == store_id)
            .options(
                selectinload(ChecklistTemplate.items),
                selectinload(ChecklistTemplate.shift),
                selectinload(ChecklistTemplate.position),
            )
        )

        if shift_id is not None:
            query = query.where(ChecklistTemplate.shift_id == shift_id)
        if position_id is not None:
            query = query.where(ChecklistTemplate.position_id == position_id)

        query = query.order_by(ChecklistTemplate.created_at.desc())
        result = await db.execute(query)
        return result.scalars().all()

    async def get_with_items(
        self,
        db: AsyncSession,
        template_id: UUID,
    ) -> ChecklistTemplate | None:
        """템플릿을 항목과 함께 조회합니다 (eager loading).

        Retrieve a template with its items eagerly loaded.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            template_id: 템플릿 UUID (Template UUID)

        Returns:
            ChecklistTemplate | None: 항목 포함 템플릿 또는 None
                                       (Template with items or None)
        """
        query: Select = (
            select(ChecklistTemplate)
            .where(ChecklistTemplate.id == template_id)
            .options(selectinload(ChecklistTemplate.items))
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def check_duplicate(
        self,
        db: AsyncSession,
        store_id: UUID,
        shift_id: UUID,
        position_id: UUID,
        exclude_id: UUID | None = None,
    ) -> bool:
        """동일 매장+근무조+포지션 조합의 중복 여부를 확인합니다.

        Check if a duplicate template exists for the store+shift+position combination.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 UUID (Store UUID)
            shift_id: 근무조 UUID (Shift UUID)
            position_id: 포지션 UUID (Position UUID)
            exclude_id: 제외할 템플릿 UUID, 수정 시 사용 (Template UUID to exclude on update)

        Returns:
            bool: 중복 존재 여부 (Whether a duplicate exists)
        """
        query: Select = (
            select(func.count())
            .select_from(ChecklistTemplate)
            .where(
                ChecklistTemplate.store_id == store_id,
                ChecklistTemplate.shift_id == shift_id,
                ChecklistTemplate.position_id == position_id,
            )
        )
        if exclude_id is not None:
            query = query.where(ChecklistTemplate.id != exclude_id)

        count: int = (await db.execute(query)).scalar() or 0
        return count > 0

    # --- 템플릿 항목 CRUD (Template item CRUD) ---

    async def get_items(
        self,
        db: AsyncSession,
        template_id: UUID,
    ) -> Sequence[ChecklistTemplateItem]:
        """템플릿의 항목 목록을 정렬 순서대로 조회합니다.

        Retrieve all items for a template ordered by sort_order.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            template_id: 템플릿 UUID (Template UUID)

        Returns:
            Sequence[ChecklistTemplateItem]: 정렬된 항목 목록 (Sorted list of items)
        """
        query: Select = (
            select(ChecklistTemplateItem)
            .where(ChecklistTemplateItem.template_id == template_id)
            .order_by(ChecklistTemplateItem.sort_order)
        )
        result = await db.execute(query)
        return result.scalars().all()

    async def create_item(
        self,
        db: AsyncSession,
        item_data: dict,
    ) -> ChecklistTemplateItem:
        """새 체크리스트 항목을 생성합니다.

        Create a new checklist template item.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            item_data: 항목 데이터 딕셔너리 (Item data dictionary)

        Returns:
            ChecklistTemplateItem: 생성된 항목 (Created item)
        """
        item: ChecklistTemplateItem = ChecklistTemplateItem(**item_data)
        db.add(item)
        await db.flush()
        await db.refresh(item)
        return item

    async def create_items_bulk(
        self,
        db: AsyncSession,
        items_data: list[dict],
    ) -> list[ChecklistTemplateItem]:
        """여러 체크리스트 항목을 일괄 생성합니다.

        Bulk-create multiple checklist template items in a single transaction.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            items_data: 항목 데이터 딕셔너리 목록 (List of item data dictionaries)

        Returns:
            list[ChecklistTemplateItem]: 생성된 항목 목록 (List of created items)
        """
        items: list[ChecklistTemplateItem] = [
            ChecklistTemplateItem(**data) for data in items_data
        ]
        db.add_all(items)
        await db.flush()
        for item in items:
            await db.refresh(item)
        return items

    async def get_item_by_id(
        self,
        db: AsyncSession,
        item_id: UUID,
    ) -> ChecklistTemplateItem | None:
        """항목 ID로 단일 항목을 조회합니다.

        Retrieve a single template item by its UUID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            item_id: 항목 UUID (Item UUID)

        Returns:
            ChecklistTemplateItem | None: 조회된 항목 또는 None (Found item or None)
        """
        query: Select = select(ChecklistTemplateItem).where(
            ChecklistTemplateItem.id == item_id
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def update_item(
        self,
        db: AsyncSession,
        item_id: UUID,
        update_data: dict,
    ) -> ChecklistTemplateItem | None:
        """기존 체크리스트 항목을 업데이트합니다.

        Update an existing checklist template item.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            item_id: 항목 UUID (Item UUID)
            update_data: 업데이트할 필드와 값 (Fields and values to update)

        Returns:
            ChecklistTemplateItem | None: 업데이트된 항목 또는 None (Updated item or None)
        """
        item: ChecklistTemplateItem | None = await self.get_item_by_id(db, item_id)
        if item is None:
            return None

        for field, value in update_data.items():
            if value is not None and hasattr(item, field):
                setattr(item, field, value)

        await db.flush()
        await db.refresh(item)
        return item

    async def delete_item(
        self,
        db: AsyncSession,
        item_id: UUID,
    ) -> bool:
        """체크리스트 항목을 삭제합니다.

        Delete a checklist template item.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            item_id: 항목 UUID (Item UUID)

        Returns:
            bool: 삭제 성공 여부 (Whether the deletion was successful)
        """
        item: ChecklistTemplateItem | None = await self.get_item_by_id(db, item_id)
        if item is None:
            return False

        await db.delete(item)
        await db.flush()
        return True

    async def reorder_items(
        self,
        db: AsyncSession,
        template_id: UUID,
        item_ids: list[UUID],
    ) -> None:
        """항목의 정렬 순서를 재배치합니다.

        Reorder template items by updating sort_order based on the provided ID list.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            template_id: 템플릿 UUID (Template UUID)
            item_ids: 새 순서대로 정렬된 항목 UUID 목록
                      (List of item UUIDs in the desired order)
        """
        for index, item_id in enumerate(item_ids):
            await db.execute(
                update(ChecklistTemplateItem)
                .where(
                    ChecklistTemplateItem.id == item_id,
                    ChecklistTemplateItem.template_id == template_id,
                )
                .values(sort_order=index)
            )
        await db.flush()


# 싱글턴 인스턴스 — Singleton instance
checklist_repository: ChecklistRepository = ChecklistRepository()
