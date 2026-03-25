"""Inventory repositories — DB queries for all inventory models."""

from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.inventory import (
    InventoryCategory, InventorySubUnit, InventoryProduct, StoreInventory,
    InventoryTransaction, InventoryAudit, InventoryAuditItem,
    InventoryAuditSetting,
)
from app.repositories.base import BaseRepository


# === Category Repository ===

class InventoryCategoryRepository(BaseRepository[InventoryCategory]):
    def __init__(self) -> None:
        super().__init__(InventoryCategory)

    async def get_tree(
        self, db: AsyncSession, organization_id: UUID
    ) -> Sequence[InventoryCategory]:
        """Get all categories for org, ordered by sort_order."""
        query = (
            select(InventoryCategory)
            .where(InventoryCategory.organization_id == organization_id)
            .order_by(InventoryCategory.sort_order)
        )
        result = await db.execute(query)
        return result.scalars().all()

    async def get_children(
        self, db: AsyncSession, parent_id: UUID
    ) -> Sequence[InventoryCategory]:
        query = (
            select(InventoryCategory)
            .where(InventoryCategory.parent_id == parent_id)
            .order_by(InventoryCategory.sort_order)
        )
        result = await db.execute(query)
        return result.scalars().all()

    async def count_products(
        self, db: AsyncSession, category_id: UUID
    ) -> int:
        """Count products using this category (as category or subcategory)."""
        query = select(func.count()).select_from(InventoryProduct).where(
            (InventoryProduct.category_id == category_id) |
            (InventoryProduct.subcategory_id == category_id)
        )
        return (await db.execute(query)).scalar() or 0


category_repository = InventoryCategoryRepository()


# === Sub Unit Repository ===

class InventorySubUnitRepository(BaseRepository[InventorySubUnit]):
    def __init__(self) -> None:
        super().__init__(InventorySubUnit)

    async def get_all_for_org(
        self, db: AsyncSession, organization_id: UUID
    ) -> Sequence[InventorySubUnit]:
        query = (
            select(InventorySubUnit)
            .where(InventorySubUnit.organization_id == organization_id)
            .order_by(InventorySubUnit.sort_order, InventorySubUnit.name)
        )
        result = await db.execute(query)
        return result.scalars().all()


sub_unit_repository = InventorySubUnitRepository()


# === Product Repository ===

class InventoryProductRepository(BaseRepository[InventoryProduct]):
    def __init__(self) -> None:
        super().__init__(InventoryProduct)

    async def search(
        self,
        db: AsyncSession,
        organization_id: UUID,
        keyword: str | None = None,
        search_field: str | None = None,
        category_id: UUID | None = None,
        is_active: bool | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[InventoryProduct], int]:
        query = (
            select(InventoryProduct)
            .where(InventoryProduct.organization_id == organization_id)
        )
        if keyword:
            kw = f"%{keyword}%"
            if search_field == "name":
                query = query.where(InventoryProduct.name.ilike(kw))
            elif search_field == "code":
                query = query.where(InventoryProduct.code.ilike(kw))
            else:  # "all" or None
                query = query.where(
                    InventoryProduct.name.ilike(kw) | InventoryProduct.code.ilike(kw)
                )
        if category_id:
            query = query.where(
                (InventoryProduct.category_id == category_id) |
                (InventoryProduct.subcategory_id == category_id)
            )
        if is_active is not None:
            query = query.where(InventoryProduct.is_active == is_active)

        query = query.order_by(InventoryProduct.name)
        return await self.get_paginated(db, query, page, per_page)

    async def generate_unique_code(
        self, db: AsyncSession, organization_id: UUID
    ) -> str:
        """Generate a unique product code: P-XXXXXXXX (8 char hex hash)."""
        import hashlib
        import time
        for _ in range(10):  # retry up to 10 times
            raw = f"{organization_id}{time.time_ns()}"
            hash_val = hashlib.sha256(raw.encode()).hexdigest()[:8].upper()
            code = f"P-{hash_val}"
            exists = await self.exists(db, {"organization_id": organization_id, "code": code})
            if not exists:
                return code
        # fallback: use full uuid segment
        import uuid as _uuid
        return f"P-{_uuid.uuid4().hex[:8].upper()}"

    async def count_stores_using(
        self, db: AsyncSession, product_id: UUID
    ) -> int:
        query = (
            select(func.count())
            .select_from(StoreInventory)
            .where(
                StoreInventory.product_id == product_id,
                StoreInventory.is_active == True,
            )
        )
        return (await db.execute(query)).scalar() or 0


product_repository = InventoryProductRepository()


# === Store Inventory Repository ===

class StoreInventoryRepository(BaseRepository[StoreInventory]):
    def __init__(self) -> None:
        super().__init__(StoreInventory)

    async def get_by_store(
        self,
        db: AsyncSession,
        store_id: UUID,
        keyword: str | None = None,
        search_field: str | None = None,
        status: str | None = None,
        is_frequent: bool | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[Sequence[StoreInventory], int]:
        query = (
            select(StoreInventory)
            .options(selectinload(StoreInventory.product))
            .where(
                StoreInventory.store_id == store_id,
                StoreInventory.is_active == True,
            )
        )
        if keyword:
            kw = f"%{keyword}%"
            if search_field == "name":
                query = query.join(InventoryProduct).where(InventoryProduct.name.ilike(kw))
            elif search_field == "code":
                query = query.join(InventoryProduct).where(InventoryProduct.code.ilike(kw))
            else:
                query = query.join(InventoryProduct).where(
                    InventoryProduct.name.ilike(kw) |
                InventoryProduct.code.ilike(f"%{keyword}%")
            )
        if is_frequent is not None:
            query = query.where(StoreInventory.is_frequent == is_frequent)

        # Ordering: frequent first, then oldest audited
        query = query.order_by(
            StoreInventory.is_frequent.desc(),
            StoreInventory.last_audited_at.asc().nulls_first(),
        )

        items, total = await self.get_paginated(db, query, page, per_page)

        # Post-filter by status (computed from current_quantity vs min_quantity)
        if status:
            filtered = []
            for item in items:
                s = self._compute_status(item)
                if s == status:
                    filtered.append(item)
            return filtered, len(filtered)

        return items, total

    @staticmethod
    def _compute_status(item: StoreInventory) -> str:
        if item.current_quantity <= 0:
            return "out"
        if item.current_quantity <= item.min_quantity:
            return "low"
        return "normal"

    async def get_by_store_and_product(
        self, db: AsyncSession, store_id: UUID, product_id: UUID
    ) -> StoreInventory | None:
        query = select(StoreInventory).where(
            StoreInventory.store_id == store_id,
            StoreInventory.product_id == product_id,
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_store_product_ids(
        self, db: AsyncSession, store_id: UUID
    ) -> set[UUID]:
        """Get set of product_ids already in this store."""
        query = select(StoreInventory.product_id).where(
            StoreInventory.store_id == store_id
        )
        result = await db.execute(query)
        return {row[0] for row in result.all()}

    async def get_stores_for_product(
        self, db: AsyncSession, product_id: UUID
    ) -> Sequence[StoreInventory]:
        query = (
            select(StoreInventory)
            .where(
                StoreInventory.product_id == product_id,
                StoreInventory.is_active == True,
            )
        )
        result = await db.execute(query)
        return result.scalars().all()

    async def get_summary(
        self, db: AsyncSession, store_id: UUID
    ) -> dict:
        """Get stock status summary for a store."""
        query = select(StoreInventory).where(
            StoreInventory.store_id == store_id,
            StoreInventory.is_active == True,
        )
        result = await db.execute(query)
        items = result.scalars().all()
        summary = {"total": len(items), "normal": 0, "low": 0, "out": 0}
        for item in items:
            s = self._compute_status(item)
            summary[s] += 1
        return summary


store_inventory_repository = StoreInventoryRepository()


# === Transaction Repository ===

class InventoryTransactionRepository(BaseRepository[InventoryTransaction]):
    def __init__(self) -> None:
        super().__init__(InventoryTransaction)

    async def get_by_store(
        self,
        db: AsyncSession,
        store_id: UUID,
        product_id: UUID | None = None,
        tx_type: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[InventoryTransaction], int]:
        query = (
            select(InventoryTransaction)
            .join(StoreInventory)
            .where(StoreInventory.store_id == store_id)
        )
        if product_id:
            query = query.where(StoreInventory.product_id == product_id)
        if tx_type:
            query = query.where(InventoryTransaction.type == tx_type)
        query = query.order_by(InventoryTransaction.created_at.desc())
        return await self.get_paginated(db, query, page, per_page)

    async def get_by_store_inventory(
        self,
        db: AsyncSession,
        store_inventory_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[InventoryTransaction], int]:
        query = (
            select(InventoryTransaction)
            .where(InventoryTransaction.store_inventory_id == store_inventory_id)
            .order_by(InventoryTransaction.created_at.desc())
        )
        return await self.get_paginated(db, query, page, per_page)


transaction_repository = InventoryTransactionRepository()


# === Audit Repository ===

class InventoryAuditRepository(BaseRepository[InventoryAudit]):
    def __init__(self) -> None:
        super().__init__(InventoryAudit)

    async def get_by_store(
        self,
        db: AsyncSession,
        store_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[InventoryAudit], int]:
        query = (
            select(InventoryAudit)
            .where(InventoryAudit.store_id == store_id)
            .order_by(InventoryAudit.created_at.desc())
        )
        return await self.get_paginated(db, query, page, per_page)

    async def get_with_items(
        self, db: AsyncSession, audit_id: UUID
    ) -> InventoryAudit | None:
        query = (
            select(InventoryAudit)
            .options(selectinload(InventoryAudit.items))
            .where(InventoryAudit.id == audit_id)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()


audit_repository = InventoryAuditRepository()


# === Audit Item Repository ===

class InventoryAuditItemRepository(BaseRepository[InventoryAuditItem]):
    def __init__(self) -> None:
        super().__init__(InventoryAuditItem)


audit_item_repository = InventoryAuditItemRepository()


# === Audit Setting Repository ===

class InventoryAuditSettingRepository(BaseRepository[InventoryAuditSetting]):
    def __init__(self) -> None:
        super().__init__(InventoryAuditSetting)

    async def get_by_store(
        self, db: AsyncSession, store_id: UUID
    ) -> InventoryAuditSetting | None:
        query = select(InventoryAuditSetting).where(
            InventoryAuditSetting.store_id == store_id
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()


audit_setting_repository = InventoryAuditSettingRepository()
