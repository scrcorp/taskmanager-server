"""체크리스트 서비스 — 체크리스트 템플릿/항목 비즈니스 로직.

Checklist Service — Business logic for checklist template and item management.
Handles template CRUD, item CRUD, reordering, and brand ownership validation.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.checklist import ChecklistTemplate, ChecklistTemplateItem
from app.models.organization import Brand
from app.repositories.checklist_repository import checklist_repository
from app.schemas.common import (
    ChecklistBulkItemCreate,
    ChecklistItemCreate,
    ChecklistItemUpdate,
    ChecklistTemplateCreate,
    ChecklistTemplateUpdate,
)
from app.utils.exceptions import DuplicateError, ForbiddenError, NotFoundError


class ChecklistService:
    """체크리스트 서비스.

    Checklist service providing template and item business logic.

    Methods handle validation, authorization, and delegate DB operations
    to the checklist repository.
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
            ForbiddenError: 다른 조직의 브랜드일 때 (When brand belongs to another org)
        """
        result = await db.execute(select(Brand).where(Brand.id == brand_id))
        brand: Brand | None = result.scalar_one_or_none()

        if brand is None:
            raise NotFoundError("브랜드를 찾을 수 없습니다 (Brand not found)")
        if brand.organization_id != organization_id:
            raise ForbiddenError("해당 브랜드에 대한 권한이 없습니다 (No permission for this brand)")

        return brand

    # --- 템플릿 CRUD (Template CRUD) ---

    async def list_all_templates(
        self,
        db: AsyncSession,
        organization_id: UUID,
        brand_id: UUID | None = None,
        shift_id: UUID | None = None,
        position_id: UUID | None = None,
    ) -> Sequence[ChecklistTemplate]:
        """조직 전체의 체크리스트 템플릿 목록을 조회합니다.

        List all checklist templates for an organization with optional filters.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            brand_id: 브랜드 UUID 필터, 선택 (Optional brand UUID filter)
            shift_id: 근무조 UUID 필터, 선택 (Optional shift UUID filter)
            position_id: 포지션 UUID 필터, 선택 (Optional position UUID filter)

        Returns:
            Sequence[ChecklistTemplate]: 템플릿 목록 (List of templates)
        """
        return await checklist_repository.get_all_by_org(
            db, organization_id, brand_id, shift_id, position_id
        )

    async def list_templates(
        self,
        db: AsyncSession,
        brand_id: UUID,
        organization_id: UUID,
        shift_id: UUID | None = None,
        position_id: UUID | None = None,
    ) -> Sequence[ChecklistTemplate]:
        """브랜드의 체크리스트 템플릿 목록을 조회합니다.

        List checklist templates for a brand with optional filters.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            brand_id: 브랜드 UUID (Brand UUID)
            organization_id: 조직 UUID (Organization UUID)
            shift_id: 근무조 UUID 필터, 선택 (Optional shift UUID filter)
            position_id: 포지션 UUID 필터, 선택 (Optional position UUID filter)

        Returns:
            Sequence[ChecklistTemplate]: 템플릿 목록 (List of templates)

        Raises:
            NotFoundError: 브랜드가 없을 때 (When brand not found)
            ForbiddenError: 다른 조직 브랜드일 때 (When brand belongs to another org)
        """
        await self._validate_brand_ownership(db, brand_id, organization_id)
        return await checklist_repository.get_by_brand(db, brand_id, shift_id, position_id)

    async def get_template_detail(
        self,
        db: AsyncSession,
        template_id: UUID,
        organization_id: UUID,
    ) -> ChecklistTemplate:
        """체크리스트 템플릿 상세를 항목과 함께 조회합니다.

        Get checklist template detail with items.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            template_id: 템플릿 UUID (Template UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            ChecklistTemplate: 항목 포함 템플릿 상세 (Template detail with items)

        Raises:
            NotFoundError: 템플릿이 없을 때 (When template not found)
            ForbiddenError: 다른 조직 브랜드일 때 (When brand belongs to another org)
        """
        template: ChecklistTemplate | None = await checklist_repository.get_with_items(
            db, template_id
        )
        if template is None:
            raise NotFoundError("체크리스트 템플릿을 찾을 수 없습니다 (Checklist template not found)")

        # 조직 소유권 검증 — Verify org ownership via brand
        await self._validate_brand_ownership(db, template.brand_id, organization_id)
        return template

    async def create_template(
        self,
        db: AsyncSession,
        brand_id: UUID,
        organization_id: UUID,
        data: ChecklistTemplateCreate,
    ) -> ChecklistTemplate:
        """새 체크리스트 템플릿을 생성합니다.

        Create a new checklist template (unique brand+shift+position).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            brand_id: 브랜드 UUID (Brand UUID)
            organization_id: 조직 UUID (Organization UUID)
            data: 템플릿 생성 데이터 (Template creation data)

        Returns:
            ChecklistTemplate: 생성된 템플릿 (Created template)

        Raises:
            NotFoundError: 브랜드가 없을 때 (When brand not found)
            ForbiddenError: 다른 조직 브랜드일 때 (When brand belongs to another org)
            DuplicateError: 동일 조합 존재 시 (When same combination already exists)
        """
        await self._validate_brand_ownership(db, brand_id, organization_id)

        shift_id: UUID = UUID(data.shift_id)
        position_id: UUID = UUID(data.position_id)

        # 중복 검사 — Check for duplicate combination
        is_duplicate: bool = await checklist_repository.check_duplicate(
            db, brand_id, shift_id, position_id
        )
        if is_duplicate:
            raise DuplicateError(
                "해당 브랜드+근무조+포지션 조합의 템플릿이 이미 존재합니다 "
                "(Template for this brand+shift+position combination already exists)"
            )

        template: ChecklistTemplate = await checklist_repository.create(
            db,
            {
                "brand_id": brand_id,
                "shift_id": shift_id,
                "position_id": position_id,
                "title": data.title,
            },
        )
        return template

    async def update_template(
        self,
        db: AsyncSession,
        template_id: UUID,
        organization_id: UUID,
        data: ChecklistTemplateUpdate,
    ) -> ChecklistTemplate:
        """체크리스트 템플릿을 업데이트합니다.

        Update a checklist template (title, shift_id, position_id).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            template_id: 템플릿 UUID (Template UUID)
            organization_id: 조직 UUID (Organization UUID)
            data: 업데이트 데이터 (Update data with optional title/shift_id/position_id)

        Returns:
            ChecklistTemplate: 업데이트된 템플릿 (Updated template)

        Raises:
            NotFoundError: 템플릿이 없을 때 (When template not found)
            ForbiddenError: 다른 조직 브랜드일 때 (When brand belongs to another org)
            DuplicateError: 동일 조합 존재 시 (When same combination already exists)
        """
        template: ChecklistTemplate | None = await checklist_repository.get_with_items(
            db, template_id
        )
        if template is None:
            raise NotFoundError("체크리스트 템플릿을 찾을 수 없습니다 (Checklist template not found)")

        await self._validate_brand_ownership(db, template.brand_id, organization_id)

        update_fields: dict = {}
        if data.title is not None:
            update_fields["title"] = data.title

        new_shift_id: UUID = UUID(data.shift_id) if data.shift_id else template.shift_id
        new_position_id: UUID = UUID(data.position_id) if data.position_id else template.position_id

        # shift_id 또는 position_id가 변경되었으면 중복 검사 — Check duplicate if shift/position changed
        if new_shift_id != template.shift_id or new_position_id != template.position_id:
            is_duplicate: bool = await checklist_repository.check_duplicate(
                db, template.brand_id, new_shift_id, new_position_id
            )
            if is_duplicate:
                raise DuplicateError(
                    "해당 브랜드+근무조+포지션 조합의 템플릿이 이미 존재합니다 "
                    "(Template for this brand+shift+position combination already exists)"
                )
            if data.shift_id is not None:
                update_fields["shift_id"] = new_shift_id
            if data.position_id is not None:
                update_fields["position_id"] = new_position_id

        if not update_fields:
            return template

        updated: ChecklistTemplate | None = await checklist_repository.update(
            db, template_id, update_fields
        )
        if updated is None:
            raise NotFoundError("체크리스트 템플릿을 찾을 수 없습니다 (Checklist template not found)")
        return updated

    async def delete_template(
        self,
        db: AsyncSession,
        template_id: UUID,
        organization_id: UUID,
    ) -> bool:
        """체크리스트 템플릿을 삭제합니다 (cascade로 항목도 삭제).

        Delete a checklist template (items are cascade-deleted).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            template_id: 템플릿 UUID (Template UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            bool: 삭제 성공 여부 (Whether the deletion was successful)

        Raises:
            NotFoundError: 템플릿이 없을 때 (When template not found)
            ForbiddenError: 다른 조직 브랜드일 때 (When brand belongs to another org)
        """
        template: ChecklistTemplate | None = await checklist_repository.get_with_items(
            db, template_id
        )
        if template is None:
            raise NotFoundError("체크리스트 템플릿을 찾을 수 없습니다 (Checklist template not found)")

        await self._validate_brand_ownership(db, template.brand_id, organization_id)
        return await checklist_repository.delete(db, template_id)

    # --- 항목 CRUD (Item CRUD) ---

    async def list_items(
        self,
        db: AsyncSession,
        template_id: UUID,
        organization_id: UUID,
    ) -> Sequence[ChecklistTemplateItem]:
        """템플릿의 항목 목록을 조회합니다.

        List items for a checklist template.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            template_id: 템플릿 UUID (Template UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            Sequence[ChecklistTemplateItem]: 정렬된 항목 목록 (Sorted item list)

        Raises:
            NotFoundError: 템플릿이 없을 때 (When template not found)
        """
        # 템플릿 존재 및 소유권 검증 — Verify template exists and ownership
        await self.get_template_detail(db, template_id, organization_id)
        return await checklist_repository.get_items(db, template_id)

    async def add_item(
        self,
        db: AsyncSession,
        template_id: UUID,
        organization_id: UUID,
        data: ChecklistItemCreate,
    ) -> ChecklistTemplateItem:
        """템플릿에 새 항목을 추가합니다.

        Add a new item to a checklist template.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            template_id: 템플릿 UUID (Template UUID)
            organization_id: 조직 UUID (Organization UUID)
            data: 항목 생성 데이터 (Item creation data)

        Returns:
            ChecklistTemplateItem: 생성된 항목 (Created item)

        Raises:
            NotFoundError: 템플릿이 없을 때 (When template not found)
        """
        await self.get_template_detail(db, template_id, organization_id)

        item: ChecklistTemplateItem = await checklist_repository.create_item(
            db,
            {
                "template_id": template_id,
                "title": data.title,
                "description": data.description,
                "verification_type": data.verification_type,
                "sort_order": data.sort_order,
            },
        )
        return item

    async def add_items_bulk(
        self,
        db: AsyncSession,
        template_id: UUID,
        organization_id: UUID,
        data: ChecklistBulkItemCreate,
    ) -> list[ChecklistTemplateItem]:
        """템플릿에 여러 항목을 일괄 추가합니다.

        Bulk-add multiple items to a checklist template in a single transaction.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            template_id: 템플릿 UUID (Template UUID)
            organization_id: 조직 UUID (Organization UUID)
            data: 일괄 생성 데이터 (Bulk creation data)

        Returns:
            list[ChecklistTemplateItem]: 생성된 항목 목록 (List of created items)

        Raises:
            NotFoundError: 템플릿이 없을 때 (When template not found)
        """
        await self.get_template_detail(db, template_id, organization_id)

        items_data: list[dict] = [
            {
                "template_id": template_id,
                "title": item.title,
                "description": item.description,
                "verification_type": item.verification_type,
                "sort_order": item.sort_order,
            }
            for item in data.items
        ]

        return await checklist_repository.create_items_bulk(db, items_data)

    async def update_item(
        self,
        db: AsyncSession,
        item_id: UUID,
        organization_id: UUID,
        data: ChecklistItemUpdate,
    ) -> ChecklistTemplateItem:
        """체크리스트 항목을 업데이트합니다.

        Update a checklist template item.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            item_id: 항목 UUID (Item UUID)
            organization_id: 조직 UUID (Organization UUID)
            data: 항목 업데이트 데이터 (Item update data)

        Returns:
            ChecklistTemplateItem: 업데이트된 항목 (Updated item)

        Raises:
            NotFoundError: 항목이 없을 때 (When item not found)
            ForbiddenError: 다른 조직의 항목일 때 (When item belongs to another org)
        """
        item: ChecklistTemplateItem | None = await checklist_repository.get_item_by_id(
            db, item_id
        )
        if item is None:
            raise NotFoundError("체크리스트 항목을 찾을 수 없습니다 (Checklist item not found)")

        # 소유권 검증 — Verify ownership via template's brand
        template: ChecklistTemplate | None = await checklist_repository.get_with_items(
            db, item.template_id
        )
        if template is None:
            raise NotFoundError("체크리스트 템플릿을 찾을 수 없습니다 (Checklist template not found)")
        await self._validate_brand_ownership(db, template.brand_id, organization_id)

        # None이 아닌 필드만 업데이트 — Only update non-None fields
        update_data: dict = data.model_dump(exclude_unset=True)
        updated: ChecklistTemplateItem | None = await checklist_repository.update_item(
            db, item_id, update_data
        )
        if updated is None:
            raise NotFoundError("체크리스트 항목을 찾을 수 없습니다 (Checklist item not found)")
        return updated

    async def reorder_items(
        self,
        db: AsyncSession,
        template_id: UUID,
        organization_id: UUID,
        item_ids: list[str],
    ) -> None:
        """항목의 정렬 순서를 재배치합니다.

        Reorder items within a template.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            template_id: 템플릿 UUID (Template UUID)
            organization_id: 조직 UUID (Organization UUID)
            item_ids: 새 순서대로 정렬된 항목 ID 문자열 목록
                      (List of item ID strings in the desired order)

        Raises:
            NotFoundError: 템플릿이 없을 때 (When template not found)
        """
        await self.get_template_detail(db, template_id, organization_id)
        uuid_ids: list[UUID] = [UUID(id_str) for id_str in item_ids]
        await checklist_repository.reorder_items(db, template_id, uuid_ids)

    async def reorder_items_by_item_id(
        self,
        db: AsyncSession,
        item_id: UUID,
        organization_id: UUID,
        item_ids: list[str],
    ) -> None:
        """항목 ID로부터 template_id를 추출하여 재배치합니다.

        Resolve template_id from an item reference and reorder items.
        Encapsulates repository access that was previously done in the router.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            item_id: 기준 항목 UUID (Reference item UUID to resolve template)
            organization_id: 조직 UUID (Organization UUID)
            item_ids: 새 순서대로 정렬된 항목 ID 문자열 목록
                      (List of item ID strings in the desired order)

        Raises:
            NotFoundError: 항목이 없을 때 (When item not found)
        """
        item: ChecklistTemplateItem | None = await checklist_repository.get_item_by_id(
            db, item_id
        )
        if item is None:
            raise NotFoundError("체크리스트 항목을 찾을 수 없습니다 (Checklist item not found)")

        await self.reorder_items(db, item.template_id, organization_id, item_ids)

    async def delete_item(
        self,
        db: AsyncSession,
        item_id: UUID,
        organization_id: UUID,
    ) -> bool:
        """체크리스트 항목을 삭제합니다.

        Delete a checklist template item.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            item_id: 항목 UUID (Item UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            bool: 삭제 성공 여부 (Whether the deletion was successful)

        Raises:
            NotFoundError: 항목이 없을 때 (When item not found)
            ForbiddenError: 다른 조직의 항목일 때 (When item belongs to another org)
        """
        item: ChecklistTemplateItem | None = await checklist_repository.get_item_by_id(
            db, item_id
        )
        if item is None:
            raise NotFoundError("체크리스트 항목을 찾을 수 없습니다 (Checklist item not found)")

        template: ChecklistTemplate | None = await checklist_repository.get_with_items(
            db, item.template_id
        )
        if template is None:
            raise NotFoundError("체크리스트 템플릿을 찾을 수 없습니다 (Checklist template not found)")
        await self._validate_brand_ownership(db, template.brand_id, organization_id)

        return await checklist_repository.delete_item(db, item_id)


# 싱글턴 인스턴스 — Singleton instance
checklist_service: ChecklistService = ChecklistService()
