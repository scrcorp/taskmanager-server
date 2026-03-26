"""Inventory service — Business logic for all inventory operations."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import (
    InventoryCategory, InventorySubUnit, InventoryProduct, StoreInventory,
    InventoryTransaction, InventoryAudit, InventoryAuditItem,
    InventoryAuditSetting,
)
from app.repositories.inventory_repository import (
    category_repository, sub_unit_repository, product_repository,
    store_inventory_repository,
    transaction_repository, audit_repository, audit_item_repository,
    audit_setting_repository,
)
from app.repositories.store_repository import store_repository
from app.schemas.inventory import (
    CategoryCreate, CategoryUpdate, CategoryResponse, CategoryTreeResponse,
    ProductCreate, ProductUpdate, ProductResponse, ProductDetailResponse,
    StoreInventoryBrief, StoreInventoryResponse, StoreInventoryUpdate,
    StoreInventoryBulkAdd,
    TransactionCreate, TransactionResponse, BulkTransactionCreate,
    AuditCreate, AuditResponse, AuditDetailResponse, AuditItemResponse,
    AuditItemsBulkUpdate,
    AuditSettingUpdate, AuditSettingResponse,
)
from app.utils.exceptions import NotFoundError, DuplicateError, ForbiddenError


# ─── Category Service ───────────────────────────────────────

class InventoryCategoryService:

    async def list_tree(
        self, db: AsyncSession, organization_id: UUID
    ) -> list[CategoryTreeResponse]:
        categories = await category_repository.get_tree(db, organization_id)
        cat_map: dict[UUID | None, list] = {}
        for c in categories:
            cat_map.setdefault(c.parent_id, []).append(c)

        async def _build(parent_id: UUID | None) -> list[CategoryTreeResponse]:
            result = []
            for c in cat_map.get(parent_id, []):
                count = await category_repository.count_products(db, c.id)
                children = await _build(c.id)
                result.append(CategoryTreeResponse(
                    id=str(c.id), organization_id=str(c.organization_id),
                    name=c.name, sort_order=c.sort_order,
                    product_count=count, children=children,
                ))
            return result

        return await _build(None)

    async def create(
        self, db: AsyncSession, organization_id: UUID, data: CategoryCreate
    ) -> CategoryResponse:
        parent_id = UUID(data.parent_id) if data.parent_id else None
        if parent_id:
            parent = await category_repository.get_by_id(db, parent_id)
            if not parent or parent.organization_id != organization_id:
                raise NotFoundError("Parent category not found")

        exists = await category_repository.exists(db, {
            "organization_id": organization_id,
            "name": data.name,
            "parent_id": parent_id,
        })
        if exists:
            raise DuplicateError("Category with this name already exists at this level")

        try:
            cat = await category_repository.create(db, {
                "organization_id": organization_id,
                "name": data.name,
                "parent_id": parent_id,
                "sort_order": data.sort_order,
            })
            await db.commit()
            count = await category_repository.count_products(db, cat.id)
            return CategoryResponse(
                id=str(cat.id), organization_id=str(cat.organization_id),
                name=cat.name, parent_id=str(cat.parent_id) if cat.parent_id else None,
                sort_order=cat.sort_order, product_count=count,
            )
        except Exception:
            await db.rollback()
            raise

    async def update(
        self, db: AsyncSession, category_id: UUID, organization_id: UUID, data: CategoryUpdate
    ) -> CategoryResponse:
        cat = await category_repository.get_by_id(db, category_id, organization_id)
        if not cat:
            raise NotFoundError("Category not found")
        update_data = data.model_dump(exclude_unset=True)
        try:
            cat = await category_repository.update(db, category_id, update_data)
            await db.commit()
            count = await category_repository.count_products(db, cat.id)
            return CategoryResponse(
                id=str(cat.id), organization_id=str(cat.organization_id),
                name=cat.name, parent_id=str(cat.parent_id) if cat.parent_id else None,
                sort_order=cat.sort_order, product_count=count,
            )
        except Exception:
            await db.rollback()
            raise

    async def delete(
        self, db: AsyncSession, category_id: UUID, organization_id: UUID
    ) -> None:
        cat = await category_repository.get_by_id(db, category_id, organization_id)
        if not cat:
            raise NotFoundError("Category not found")
        children = await category_repository.get_children(db, category_id)
        if children:
            raise ForbiddenError("Cannot delete category with subcategories")
        product_count = await category_repository.count_products(db, category_id)
        if product_count > 0:
            raise ForbiddenError("Cannot delete category with linked products")
        try:
            await category_repository.delete(db, category_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise


category_service = InventoryCategoryService()


# ─── Sub Unit Service ───────────────────────────────────────

class InventorySubUnitService:

    async def list_sub_units(
        self, db: AsyncSession, organization_id: UUID
    ) -> list[dict]:
        units = await sub_unit_repository.get_all_for_org(db, organization_id)
        return [{"id": str(u.id), "name": u.name, "code": u.code, "sort_order": u.sort_order} for u in units]

    async def create(
        self, db: AsyncSession, organization_id: UUID, name: str, code: str | None = None
    ) -> dict:
        effective_code = (code or name).strip().lower()
        exists = await sub_unit_repository.exists(db, {
            "organization_id": organization_id, "code": effective_code,
        })
        if exists:
            raise DuplicateError(f"Sub unit '{effective_code}' already exists")
        try:
            unit = await sub_unit_repository.create(db, {
                "organization_id": organization_id,
                "name": name.strip(),
                "code": effective_code,
            })
            await db.commit()
            return {"id": str(unit.id), "name": unit.name, "code": unit.code, "sort_order": unit.sort_order}
        except Exception:
            await db.rollback()
            raise

    async def update(
        self, db: AsyncSession, sub_unit_id: UUID, organization_id: UUID, name: str
    ) -> dict:
        unit = await sub_unit_repository.get_by_id(db, sub_unit_id)
        if not unit or unit.organization_id != organization_id:
            raise NotFoundError("Sub unit not found")
        try:
            unit = await sub_unit_repository.update(db, sub_unit_id, {"name": name.strip()})
            await db.commit()
            return {"id": str(unit.id), "name": unit.name, "code": unit.code, "sort_order": unit.sort_order}
        except Exception:
            await db.rollback()
            raise

    async def delete(
        self, db: AsyncSession, sub_unit_id: UUID, organization_id: UUID
    ) -> None:
        unit = await sub_unit_repository.get_by_id(db, sub_unit_id)
        if not unit or unit.organization_id != organization_id:
            raise NotFoundError("Sub unit not found")
        # Check if any products use this sub_unit
        count_query = sa_select(func.count()).select_from(InventoryProduct).where(
            InventoryProduct.sub_unit == unit.code,
            InventoryProduct.organization_id == organization_id,
        )
        count = (await db.execute(count_query)).scalar() or 0
        if count > 0:
            raise ForbiddenError(f"Cannot delete: {count} product(s) use this sub unit")
        try:
            await sub_unit_repository.delete(db, sub_unit_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise


sub_unit_service = InventorySubUnitService()


# ─── Product Service ────────────────────────────────────────

class InventoryProductService:

    def _to_response(
        self, p: InventoryProduct, store_count: int = 0,
        cat_name: str | None = None, subcat_name: str | None = None,
        image_url: str | None = None,
    ) -> ProductResponse:
        return ProductResponse(
            id=str(p.id), organization_id=str(p.organization_id),
            name=p.name, code=p.code, barcode=p.barcode,
            category_id=str(p.category_id) if p.category_id else None,
            category_name=cat_name,
            subcategory_id=str(p.subcategory_id) if p.subcategory_id else None,
            subcategory_name=subcat_name,
            sub_unit=p.sub_unit, sub_unit_ratio=p.sub_unit_ratio,
            image_url=image_url or p.image_url,
            description=p.description, is_active=p.is_active,
            store_count=store_count,
        )

    async def _resolve_category_id(
        self, db: AsyncSession, organization_id: UUID, value: str
    ) -> UUID | None:
        """Resolve category_id from UUID string or category name.
        If value is a valid UUID, use it directly. If not, treat as name and find/create."""
        if not value:
            return None
        try:
            return UUID(value)
        except ValueError:
            # Not a UUID — treat as category name, find or create
            cats = await category_repository.get_tree(db, organization_id)
            for c in cats:
                if c.name.lower() == value.lower():
                    return c.id
            # Auto-create
            new_cat = await category_repository.create(db, {
                "organization_id": organization_id,
                "name": value.strip(),
                "parent_id": None,
                "sort_order": 0,
            })
            return new_cat.id

    async def _get_category_name(self, db: AsyncSession, cat_id: UUID | None) -> str | None:
        if not cat_id:
            return None
        cat = await category_repository.get_by_id(db, cat_id)
        return cat.name if cat else None

    async def _enrich_response(self, db: AsyncSession, p: InventoryProduct) -> ProductResponse:
        store_count = await product_repository.count_stores_using(db, p.id)
        cat_name = await self._get_category_name(db, p.category_id)
        subcat_name = await self._get_category_name(db, p.subcategory_id)
        return self._to_response(p, store_count, cat_name, subcat_name)

    async def list_products(
        self, db: AsyncSession, organization_id: UUID,
        keyword: str | None = None, search_field: str | None = None,
        category_id: str | None = None,
        is_active: bool | None = None, page: int = 1, per_page: int = 20,
    ) -> tuple[list[ProductResponse], int]:
        cat_uuid = UUID(category_id) if category_id else None
        products, total = await product_repository.search(
            db, organization_id, keyword, search_field, cat_uuid, is_active, page, per_page
        )
        results = [await self._enrich_response(db, p) for p in products]
        return results, total

    async def get_product(
        self, db: AsyncSession, product_id: UUID, organization_id: UUID
    ) -> ProductDetailResponse:
        p = await product_repository.get_by_id(db, product_id, organization_id)
        if not p:
            raise NotFoundError("Product not found")
        base = await self._enrich_response(db, p)
        store_items = await store_inventory_repository.get_stores_for_product(db, product_id)
        stores = []
        for si in store_items:
            store = await store_repository.get_by_id(db, si.store_id)
            stores.append(StoreInventoryBrief(
                store_id=str(si.store_id),
                store_name=store.name if store else "Unknown",
                current_quantity=si.current_quantity,
                min_quantity=si.min_quantity,
                is_frequent=si.is_frequent,
            ))
        return ProductDetailResponse(**base.model_dump(), stores=stores)

    async def create_product(
        self, db: AsyncSession, organization_id: UUID, data: ProductCreate, created_by: UUID
    ) -> ProductResponse:
        # Validate sub_unit consistency
        if data.sub_unit and not data.sub_unit_ratio:
            raise ForbiddenError("sub_unit_ratio is required when sub_unit is set")

        # Auto-generate code if not provided
        code = data.code
        if not code:
            code = await product_repository.generate_unique_code(db, organization_id)

        # Check code uniqueness
        exists = await product_repository.exists(db, {
            "organization_id": organization_id, "code": code,
        })
        if exists:
            raise DuplicateError(f"Product code '{code}' already exists")

        try:
            product = await product_repository.create(db, {
                "organization_id": organization_id,
                "name": data.name,
                "code": code,
                "category_id": await self._resolve_category_id(db, organization_id, data.category_id) if data.category_id else None,
                "subcategory_id": await self._resolve_category_id(db, organization_id, data.subcategory_id) if data.subcategory_id else None,
                "sub_unit": data.sub_unit,
                "sub_unit_ratio": data.sub_unit_ratio,
                "image_url": data.image_url,
                "description": data.description,
            })

            # Create store inventory entries if stores provided
            for store_entry in data.stores:
                sid = UUID(store_entry.store_id)
                si = StoreInventory(
                    store_id=sid,
                    product_id=product.id,
                    current_quantity=store_entry.initial_quantity,
                    min_quantity=store_entry.min_quantity,
                    is_frequent=store_entry.is_frequent,
                )
                db.add(si)
                await db.flush()
                # Create initial stock_in transaction if initial_quantity > 0
                if store_entry.initial_quantity > 0:
                    tx = InventoryTransaction(
                        store_inventory_id=si.id,
                        type="stock_in",
                        quantity=store_entry.initial_quantity,
                        before_quantity=0,
                        after_quantity=store_entry.initial_quantity,
                        reason="Initial stock on product registration",
                        created_by=created_by,
                    )
                    db.add(tx)

            await db.commit()
            return await self._enrich_response(db, product)
        except Exception:
            await db.rollback()
            raise

    async def update_product(
        self, db: AsyncSession, product_id: UUID, organization_id: UUID, data: ProductUpdate
    ) -> ProductResponse:
        p = await product_repository.get_by_id(db, product_id, organization_id)
        if not p:
            raise NotFoundError("Product not found")

        update_data = data.model_dump(exclude_unset=True)

        # Convert string IDs to UUIDs
        for field in ("category_id", "subcategory_id"):
            if field in update_data and update_data[field]:
                update_data[field] = UUID(update_data[field])

        # Code uniqueness check if changing
        if "code" in update_data and update_data["code"] != p.code:
            exists = await product_repository.exists(db, {
                "organization_id": organization_id, "code": update_data["code"],
            })
            if exists:
                raise DuplicateError(f"Product code '{update_data['code']}' already exists")

        try:
            product = await product_repository.update(db, product_id, update_data)
            await db.commit()
            return await self._enrich_response(db, product)
        except Exception:
            await db.rollback()
            raise

    async def deactivate_product(
        self, db: AsyncSession, product_id: UUID, organization_id: UUID
    ) -> None:
        p = await product_repository.get_by_id(db, product_id, organization_id)
        if not p:
            raise NotFoundError("Product not found")
        try:
            await product_repository.update(db, product_id, {"is_active": False})
            await db.commit()
        except Exception:
            await db.rollback()
            raise


    async def preview_code(self, db: AsyncSession, organization_id: UUID) -> str:
        """Generate a preview of what the auto-generated code would be."""
        return await product_repository.generate_unique_code(db, organization_id)

    async def import_from_excel(
        self, db: AsyncSession, organization_id: UUID, rows: list[dict], created_by: UUID
    ) -> dict:
        """Import products from parsed excel rows.

        Each row: { name, code?, category?, subcategory?, sub_unit?, sub_unit_ratio?,
                    description?, store_code?, min_quantity?, initial_quantity?, is_frequent? }

        Returns: { created: int, linked: int, skipped: [], errors: [] }
        """
        from app.models.organization import Store
        from sqlalchemy import select as sa_select

        result = {"created": 0, "linked": 0, "skipped": [], "errors": []}

        try:
            for i, row in enumerate(rows):
                row_num = i + 2  # excel row (1-indexed header + data)
                name = (row.get("name") or "").strip()
                if not name:
                    result["errors"].append(f"Row {row_num}: name is required")
                    continue

                code = (row.get("code") or "").strip() or None
                store_code = (row.get("store_code") or "").strip() or None

                # Resolve store by code
                store_id = None
                if store_code:
                    store_query = sa_select(Store).where(
                        Store.organization_id == organization_id,
                        Store.code == store_code,
                    )
                    store_result = await db.execute(store_query)
                    store = store_result.scalar_one_or_none()
                    if not store:
                        result["errors"].append(f"Row {row_num}: store code '{store_code}' not found")
                        continue
                    store_id = store.id

                # Resolve category
                category_id = None
                cat_name = (row.get("category") or "").strip()
                if cat_name:
                    cats = await category_repository.get_tree(db, organization_id)
                    for c in cats:
                        if c.name.lower() == cat_name.lower() and c.parent_id is None:
                            category_id = c.id
                            break
                    if not category_id:
                        # Auto-create category
                        new_cat = await category_repository.create(db, {
                            "organization_id": organization_id,
                            "name": cat_name,
                            "parent_id": None,
                            "sort_order": 0,
                        })
                        category_id = new_cat.id

                # Resolve subcategory
                subcategory_id = None
                subcat_name = (row.get("subcategory") or "").strip()
                if subcat_name and category_id:
                    children = await category_repository.get_children(db, category_id)
                    for c in children:
                        if c.name.lower() == subcat_name.lower():
                            subcategory_id = c.id
                            break
                    if not subcategory_id:
                        new_sub = await category_repository.create(db, {
                            "organization_id": organization_id,
                            "name": subcat_name,
                            "parent_id": category_id,
                            "sort_order": 0,
                        })
                        subcategory_id = new_sub.id

                # Check if product code already exists
                existing_product = None
                if code:
                    existing_check = await product_repository.exists(db, {
                        "organization_id": organization_id, "code": code,
                    })
                    if existing_check:
                        # Find the existing product
                        from sqlalchemy import select as _sel
                        eq = _sel(InventoryProduct).where(
                            InventoryProduct.organization_id == organization_id,
                            InventoryProduct.code == code,
                        )
                        er = await db.execute(eq)
                        existing_product = er.scalar_one_or_none()

                if existing_product:
                    # Link existing product to store
                    if store_id:
                        existing_si = await store_inventory_repository.get_by_store_and_product(
                            db, store_id, existing_product.id
                        )
                        if not existing_si:
                            min_qty = int(row.get("min_quantity") or 0)
                            init_qty = int(row.get("initial_quantity") or 0)
                            is_freq = str(row.get("is_frequent") or "").lower() in ("true", "yes", "1", "y")
                            si = StoreInventory(
                                store_id=store_id, product_id=existing_product.id,
                                current_quantity=init_qty, min_quantity=min_qty,
                                is_frequent=is_freq,
                            )
                            db.add(si)
                            await db.flush()
                            if init_qty > 0:
                                tx = InventoryTransaction(
                                    store_inventory_id=si.id, type="stock_in",
                                    quantity=init_qty, before_quantity=0,
                                    after_quantity=init_qty,
                                    reason="Excel import initial stock",
                                    created_by=created_by,
                                )
                                db.add(tx)
                            result["linked"] += 1
                        else:
                            result["skipped"].append(f"Row {row_num}: '{name}' already in store")
                    else:
                        result["skipped"].append(f"Row {row_num}: product '{code}' exists, no store_code to link")
                else:
                    # Create new product
                    if not code:
                        code = await product_repository.generate_unique_code(db, organization_id)

                    sub_unit = (row.get("sub_unit") or "").strip() or None
                    sub_unit_ratio = None
                    if sub_unit:
                        try:
                            sub_unit_ratio = int(row.get("sub_unit_ratio") or 0)
                        except (ValueError, TypeError):
                            sub_unit_ratio = None

                    product = await product_repository.create(db, {
                        "organization_id": organization_id,
                        "name": name,
                        "code": code,
                        "category_id": category_id,
                        "subcategory_id": subcategory_id,
                        "sub_unit": sub_unit,
                        "sub_unit_ratio": sub_unit_ratio if sub_unit_ratio and sub_unit_ratio > 0 else None,
                        "description": (row.get("description") or "").strip() or None,
                    })
                    result["created"] += 1

                    # Link to store if store_code provided
                    if store_id:
                        min_qty = int(row.get("min_quantity") or 0)
                        init_qty = int(row.get("initial_quantity") or 0)
                        is_freq = str(row.get("is_frequent") or "").lower() in ("true", "yes", "1", "y")
                        si = StoreInventory(
                            store_id=store_id, product_id=product.id,
                            current_quantity=init_qty, min_quantity=min_qty,
                            is_frequent=is_freq,
                        )
                        db.add(si)
                        await db.flush()
                        if init_qty > 0:
                            tx = InventoryTransaction(
                                store_inventory_id=si.id, type="stock_in",
                                quantity=init_qty, before_quantity=0,
                                after_quantity=init_qty,
                                reason="Excel import initial stock",
                                created_by=created_by,
                            )
                            db.add(tx)
                        result["linked"] += 1

            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise


product_service = InventoryProductService()


# ─── Store Inventory Service ────────────────────────────────

class StoreInventoryService:

    def _to_response(self, si: StoreInventory, product: InventoryProduct | None = None) -> StoreInventoryResponse:
        p = product or si.product
        status = "out" if si.current_quantity <= 0 else ("low" if si.current_quantity <= si.min_quantity else "normal")
        return StoreInventoryResponse(
            id=str(si.id), store_id=str(si.store_id), product_id=str(si.product_id),
            product_name=p.name if p else "Unknown",
            product_code=p.code if p else "",
            category_name=None,  # filled by caller if needed
            sub_unit=p.sub_unit if p else None,
            sub_unit_ratio=p.sub_unit_ratio if p else None,
            image_url=p.image_url if p else None,
            description=p.description if p else None,
            current_quantity=si.current_quantity, min_quantity=si.min_quantity,
            is_frequent=si.is_frequent,
            last_audited_at=si.last_audited_at.isoformat() if si.last_audited_at else None,
            is_active=si.is_active, status=status,
        )

    async def list_inventory(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID,
        keyword: str | None = None, search_field: str | None = None,
        status: str | None = None,
        is_frequent: bool | None = None, page: int = 1, per_page: int = 50,
    ) -> tuple[list[StoreInventoryResponse], int]:
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if not store:
            raise NotFoundError("Store not found")
        items, total = await store_inventory_repository.get_by_store(
            db, store_id, keyword, search_field, status, is_frequent, page, per_page
        )
        results = []
        for si in items:
            product = si.product if hasattr(si, 'product') and si.product else await product_repository.get_by_id(db, si.product_id)
            results.append(self._to_response(si, product))
        return results, total

    async def get_summary(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID
    ) -> dict:
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if not store:
            raise NotFoundError("Store not found")
        return await store_inventory_repository.get_summary(db, store_id)

    async def bulk_add(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID,
        data: StoreInventoryBulkAdd, created_by: UUID,
    ) -> list[StoreInventoryResponse]:
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if not store:
            raise NotFoundError("Store not found")

        results = []
        try:
            for item in data.items:
                pid = UUID(item.product_id)
                existing = await store_inventory_repository.get_by_store_and_product(db, store_id, pid)
                if existing:
                    continue  # skip already added

                product = await product_repository.get_by_id(db, pid, organization_id)
                if not product:
                    continue

                si = StoreInventory(
                    store_id=store_id, product_id=pid,
                    current_quantity=item.initial_quantity,
                    min_quantity=item.min_quantity,
                    is_frequent=item.is_frequent,
                )
                db.add(si)
                await db.flush()
                await db.refresh(si)

                if item.initial_quantity > 0:
                    tx = InventoryTransaction(
                        store_inventory_id=si.id, type="stock_in",
                        quantity=item.initial_quantity,
                        before_quantity=0, after_quantity=item.initial_quantity,
                        reason="Initial stock on store registration",
                        created_by=created_by,
                    )
                    db.add(tx)

                results.append(self._to_response(si, product))

            await db.commit()
            return results
        except Exception:
            await db.rollback()
            raise

    async def update_item(
        self, db: AsyncSession, item_id: UUID, store_id: UUID,
        organization_id: UUID, data: StoreInventoryUpdate,
    ) -> StoreInventoryResponse:
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if not store:
            raise NotFoundError("Store not found")
        si = await store_inventory_repository.get_by_id(db, item_id)
        if not si or si.store_id != store_id:
            raise NotFoundError("Store inventory item not found")
        try:
            update_data = data.model_dump(exclude_unset=True)
            si = await store_inventory_repository.update(db, item_id, update_data)
            await db.commit()
            product = await product_repository.get_by_id(db, si.product_id)
            return self._to_response(si, product)
        except Exception:
            await db.rollback()
            raise

    async def deactivate_item(
        self, db: AsyncSession, item_id: UUID, store_id: UUID, organization_id: UUID
    ) -> None:
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if not store:
            raise NotFoundError("Store not found")
        si = await store_inventory_repository.get_by_id(db, item_id)
        if not si or si.store_id != store_id:
            raise NotFoundError("Store inventory item not found")
        try:
            await store_inventory_repository.update(db, item_id, {"is_active": False})
            await db.commit()
        except Exception:
            await db.rollback()
            raise


store_inventory_service = StoreInventoryService()


# ─── Transaction Service ────────────────────────────────────

class InventoryTransactionService:

    def _to_response(self, tx: InventoryTransaction, product_name: str = "", product_code: str = "", user_name: str | None = None) -> TransactionResponse:
        return TransactionResponse(
            id=str(tx.id), store_inventory_id=str(tx.store_inventory_id),
            product_name=product_name, product_code=product_code,
            type=tx.type, quantity=tx.quantity,
            before_quantity=tx.before_quantity, after_quantity=tx.after_quantity,
            reason=tx.reason,
            created_by=str(tx.created_by),
            created_by_name=user_name,
            created_at=tx.created_at.isoformat(),
        )

    async def create_transaction(
        self, db: AsyncSession, store_inventory_id: UUID, store_id: UUID,
        organization_id: UUID, data: TransactionCreate, created_by: UUID,
    ) -> TransactionResponse:
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if not store:
            raise NotFoundError("Store not found")
        si = await store_inventory_repository.get_by_id(db, store_inventory_id)
        if not si or si.store_id != store_id:
            raise NotFoundError("Store inventory item not found")

        before = si.current_quantity
        change = data.quantity if data.type == "stock_in" else -data.quantity
        after = before + change

        try:
            tx = InventoryTransaction(
                store_inventory_id=store_inventory_id,
                type=data.type, quantity=change,
                before_quantity=before, after_quantity=after,
                reason=data.reason, created_by=created_by,
            )
            db.add(tx)
            si.current_quantity = after
            await db.flush()
            await db.commit()

            product = await product_repository.get_by_id(db, si.product_id)
            return self._to_response(tx, product.name if product else "", product.code if product else "")
        except Exception:
            await db.rollback()
            raise

    async def bulk_transaction(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID,
        data: BulkTransactionCreate, created_by: UUID,
    ) -> list[TransactionResponse]:
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if not store:
            raise NotFoundError("Store not found")

        results = []
        try:
            for item in data.items:
                si_id = UUID(item.store_inventory_id)
                si = await store_inventory_repository.get_by_id(db, si_id)
                if not si or si.store_id != store_id:
                    continue

                before = si.current_quantity
                change = item.quantity if data.type == "stock_in" else -item.quantity
                after = before + change
                reason = item.reason or data.reason

                tx = InventoryTransaction(
                    store_inventory_id=si_id,
                    type=data.type, quantity=change,
                    before_quantity=before, after_quantity=after,
                    reason=reason, created_by=created_by,
                )
                db.add(tx)
                si.current_quantity = after
                await db.flush()

                product = await product_repository.get_by_id(db, si.product_id)
                results.append(self._to_response(tx, product.name if product else "", product.code if product else ""))

            await db.commit()
            return results
        except Exception:
            await db.rollback()
            raise

    async def list_transactions(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID,
        product_id: str | None = None, tx_type: str | None = None,
        page: int = 1, per_page: int = 20,
    ) -> tuple[list[TransactionResponse], int]:
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if not store:
            raise NotFoundError("Store not found")
        pid = UUID(product_id) if product_id else None
        txs, total = await transaction_repository.get_by_store(db, store_id, pid, tx_type, page, per_page)
        results = []
        for tx in txs:
            si = await store_inventory_repository.get_by_id(db, tx.store_inventory_id)
            product = await product_repository.get_by_id(db, si.product_id) if si else None
            results.append(self._to_response(tx, product.name if product else "", product.code if product else ""))
        return results, total


    async def adjust_stock(
        self, db: AsyncSession, store_inventory_id: UUID, store_id: UUID,
        organization_id: UUID, actual_quantity: int, reason: str | None,
        created_by: UUID,
    ) -> TransactionResponse:
        """Set current quantity to actual_quantity. Creates adjustment transaction."""
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if not store:
            raise NotFoundError("Store not found")
        si = await store_inventory_repository.get_by_id(db, store_inventory_id)
        if not si or si.store_id != store_id:
            raise NotFoundError("Store inventory item not found")

        before = si.current_quantity
        difference = actual_quantity - before
        if difference == 0:
            # No change needed, but still return a response
            product = await product_repository.get_by_id(db, si.product_id)
            return self._to_response(
                InventoryTransaction(
                    store_inventory_id=store_inventory_id, type="adjustment",
                    quantity=0, before_quantity=before, after_quantity=before,
                    reason=reason or "No change", created_by=created_by,
                ),
                product.name if product else "", product.code if product else "",
            )

        try:
            tx = InventoryTransaction(
                store_inventory_id=store_inventory_id,
                type="adjustment", quantity=difference,
                before_quantity=before, after_quantity=actual_quantity,
                reason=reason or "Manual adjustment",
                created_by=created_by,
            )
            db.add(tx)
            si.current_quantity = actual_quantity
            await db.flush()
            await db.commit()

            product = await product_repository.get_by_id(db, si.product_id)
            return self._to_response(tx, product.name if product else "", product.code if product else "")
        except Exception:
            await db.rollback()
            raise


transaction_service = InventoryTransactionService()


# ─── Audit Service ──────────────────────────────────────────

class InventoryAuditService:

    async def start_audit(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID,
        data: AuditCreate, audited_by: UUID,
    ) -> AuditDetailResponse:
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if not store:
            raise NotFoundError("Store not found")

        try:
            audit = InventoryAudit(
                store_id=store_id, audited_by=audited_by,
                status="in_progress", note=data.note,
            )
            db.add(audit)
            await db.flush()
            await db.refresh(audit)

            # Snapshot all active store inventory items
            items, _ = await store_inventory_repository.get_by_store(db, store_id, per_page=9999)
            audit_items = []
            for si in items:
                ai = InventoryAuditItem(
                    audit_id=audit.id,
                    store_inventory_id=si.id,
                    system_quantity=si.current_quantity,
                    actual_quantity=si.current_quantity,  # pre-fill with system qty
                    difference=0,
                )
                db.add(ai)
                await db.flush()
                await db.refresh(ai)

                product = si.product if hasattr(si, 'product') and si.product else await product_repository.get_by_id(db, si.product_id)
                audit_items.append(AuditItemResponse(
                    id=str(ai.id), store_inventory_id=str(ai.store_inventory_id),
                    product_name=product.name if product else "",
                    product_code=product.code if product else "",
                    system_quantity=ai.system_quantity,
                    actual_quantity=ai.actual_quantity,
                    difference=ai.difference,
                    is_frequent=si.is_frequent,
                ))

            await db.commit()
            return AuditDetailResponse(
                id=str(audit.id), store_id=str(audit.store_id),
                audited_by=str(audit.audited_by), auditor_name=None,
                status=audit.status,
                started_at=audit.started_at.isoformat(),
                completed_at=None, note=audit.note,
                items_count=len(audit_items), discrepancies=0,
                items=audit_items,
            )
        except Exception:
            await db.rollback()
            raise

    async def update_audit_items(
        self, db: AsyncSession, audit_id: UUID, store_id: UUID,
        organization_id: UUID, data: AuditItemsBulkUpdate,
    ) -> AuditDetailResponse:
        audit = await audit_repository.get_by_id(db, audit_id)
        if not audit or audit.store_id != store_id:
            raise NotFoundError("Audit not found")
        if audit.status != "in_progress":
            raise ForbiddenError("Audit is already completed")

        try:
            for item_data in data.items:
                si_id = UUID(item_data.store_inventory_id)
                # Find the audit item
                from sqlalchemy import select as sa_select
                from app.models.inventory import InventoryAuditItem as AuditItemModel
                query = sa_select(AuditItemModel).where(
                    AuditItemModel.audit_id == audit_id,
                    AuditItemModel.store_inventory_id == si_id,
                )
                result = await db.execute(query)
                ai = result.scalar_one_or_none()
                if ai:
                    ai.actual_quantity = item_data.actual_quantity
                    ai.difference = item_data.actual_quantity - ai.system_quantity

            await db.flush()
            await db.commit()
            return await self.get_audit_detail(db, audit_id, store_id, organization_id)
        except Exception:
            await db.rollback()
            raise

    async def complete_audit(
        self, db: AsyncSession, audit_id: UUID, store_id: UUID,
        organization_id: UUID, completed_by: UUID,
    ) -> AuditDetailResponse:
        audit = await audit_repository.get_with_items(db, audit_id)
        if not audit or audit.store_id != store_id:
            raise NotFoundError("Audit not found")
        if audit.status != "in_progress":
            raise ForbiddenError("Audit is already completed")

        now = datetime.now(timezone.utc)
        try:
            for ai in audit.items:
                if ai.difference != 0:
                    si = await store_inventory_repository.get_by_id(db, ai.store_inventory_id)
                    if si:
                        before = si.current_quantity
                        si.current_quantity = ai.actual_quantity
                        tx = InventoryTransaction(
                            store_inventory_id=si.id,
                            type="adjustment", quantity=ai.difference,
                            before_quantity=before, after_quantity=ai.actual_quantity,
                            reason="Audit adjustment",
                            audit_id=audit_id, created_by=completed_by,
                        )
                        db.add(tx)

                # Update last_audited_at for the store inventory item
                si = await store_inventory_repository.get_by_id(db, ai.store_inventory_id)
                if si:
                    si.last_audited_at = now

            audit.status = "completed"
            audit.completed_at = now
            await db.flush()
            await db.commit()
            return await self.get_audit_detail(db, audit_id, store_id, organization_id)
        except Exception:
            await db.rollback()
            raise

    async def get_audit_detail(
        self, db: AsyncSession, audit_id: UUID, store_id: UUID, organization_id: UUID
    ) -> AuditDetailResponse:
        audit = await audit_repository.get_with_items(db, audit_id)
        if not audit or audit.store_id != store_id:
            raise NotFoundError("Audit not found")

        audit_items = []
        discrepancies = 0
        for ai in audit.items:
            si = await store_inventory_repository.get_by_id(db, ai.store_inventory_id)
            product = await product_repository.get_by_id(db, si.product_id) if si else None
            if ai.difference != 0:
                discrepancies += 1
            audit_items.append(AuditItemResponse(
                id=str(ai.id), store_inventory_id=str(ai.store_inventory_id),
                product_name=product.name if product else "",
                product_code=product.code if product else "",
                system_quantity=ai.system_quantity,
                actual_quantity=ai.actual_quantity,
                difference=ai.difference,
                is_frequent=si.is_frequent if si else False,
            ))

        return AuditDetailResponse(
            id=str(audit.id), store_id=str(audit.store_id),
            audited_by=str(audit.audited_by), auditor_name=None,
            status=audit.status,
            started_at=audit.started_at.isoformat(),
            completed_at=audit.completed_at.isoformat() if audit.completed_at else None,
            note=audit.note,
            items_count=len(audit_items), discrepancies=discrepancies,
            items=audit_items,
        )

    async def list_audits(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID,
        page: int = 1, per_page: int = 20,
    ) -> tuple[list[AuditResponse], int]:
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if not store:
            raise NotFoundError("Store not found")
        audits, total = await audit_repository.get_by_store(db, store_id, page, per_page)
        results = []
        for a in audits:
            # Count items and discrepancies
            audit_with_items = await audit_repository.get_with_items(db, a.id)
            disc = sum(1 for ai in audit_with_items.items if ai.difference != 0) if audit_with_items else 0
            item_count = len(audit_with_items.items) if audit_with_items else 0
            results.append(AuditResponse(
                id=str(a.id), store_id=str(a.store_id),
                audited_by=str(a.audited_by), auditor_name=None,
                status=a.status,
                started_at=a.started_at.isoformat(),
                completed_at=a.completed_at.isoformat() if a.completed_at else None,
                note=a.note,
                items_count=item_count, discrepancies=disc,
            ))
        return results, total


audit_service = InventoryAuditService()


# ─── Audit Settings Service ─────────────────────────────────

class InventoryAuditSettingService:

    async def get_setting(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID
    ) -> AuditSettingResponse:
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if not store:
            raise NotFoundError("Store not found")
        setting = await audit_setting_repository.get_by_store(db, store_id)
        if not setting:
            # Return default
            return AuditSettingResponse(
                id="", store_id=str(store_id), frequency="daily", day_of_week=None
            )
        return AuditSettingResponse(
            id=str(setting.id), store_id=str(setting.store_id),
            frequency=setting.frequency, day_of_week=setting.day_of_week,
        )

    async def update_setting(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID,
        data: AuditSettingUpdate,
    ) -> AuditSettingResponse:
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if not store:
            raise NotFoundError("Store not found")

        try:
            setting = await audit_setting_repository.get_by_store(db, store_id)
            if setting:
                setting.frequency = data.frequency
                setting.day_of_week = data.day_of_week
                await db.flush()
            else:
                setting = InventoryAuditSetting(
                    store_id=store_id,
                    frequency=data.frequency,
                    day_of_week=data.day_of_week,
                )
                db.add(setting)
                await db.flush()
                await db.refresh(setting)

            await db.commit()
            return AuditSettingResponse(
                id=str(setting.id), store_id=str(setting.store_id),
                frequency=setting.frequency, day_of_week=setting.day_of_week,
            )
        except Exception:
            await db.rollback()
            raise


audit_setting_service = InventoryAuditSettingService()
