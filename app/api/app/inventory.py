"""App inventory router — Store inventory, stock in/out, audit for staff app.

SV+ can manage inventory, Staff can only view.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.inventory import (
    ProductCreate, ProductResponse,
    StoreInventoryBulkAdd, StoreInventoryResponse,
    TransactionCreate, TransactionResponse, BulkTransactionCreate,
    AuditCreate, AuditDetailResponse, AuditItemsBulkUpdate,
)
from app.services.inventory_service import (
    category_service, sub_unit_service, product_service,
    store_inventory_service, transaction_service, audit_service,
)

router = APIRouter()


# ═══════════════════════════════════════════════════
# Categories + Sub Units (read-only for app)
# ═══════════════════════════════════════════════════

@router.get("/inventory/categories")
async def list_categories(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
) -> list:
    return await category_service.list_tree(db, current_user.organization_id)


@router.get("/inventory/sub-units")
async def list_sub_units(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
) -> list:
    return await sub_unit_service.list_sub_units(db, current_user.organization_id)


# ═══════════════════════════════════════════════════
# Store Inventory (view: Staff+, manage: SV+)
# ═══════════════════════════════════════════════════

@router.get("/stores/{store_id}/inventory")
async def list_store_inventory(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
    keyword: str | None = None,
    search_field: str | None = None,
    status: str | None = None,
    is_frequent: bool | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    await check_store_access(db, current_user, store_id)
    items, total = await store_inventory_service.list_inventory(
        db, store_id, current_user.organization_id,
        keyword, search_field, status, is_frequent, page, per_page,
    )
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/stores/{store_id}/inventory/summary")
async def get_store_inventory_summary(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
) -> dict:
    await check_store_access(db, current_user, store_id)
    return await store_inventory_service.get_summary(db, store_id, current_user.organization_id)


@router.post("/stores/{store_id}/inventory", status_code=201)
async def add_products_to_store(
    store_id: UUID,
    data: StoreInventoryBulkAdd,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:create"))],
) -> list[StoreInventoryResponse]:
    await check_store_access(db, current_user, store_id)
    return await store_inventory_service.bulk_add(
        db, store_id, current_user.organization_id, data, current_user.id
    )


# ═══════════════════════════════════════════════════
# Product search + create (for add-product flow)
# ═══════════════════════════════════════════════════

@router.get("/inventory/products")
async def search_products(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
    keyword: str | None = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    items, total = await product_service.list_products(
        db, current_user.organization_id, keyword, page=page, per_page=per_page
    )
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.post("/inventory/products", response_model=ProductResponse, status_code=201)
async def create_product(
    data: ProductCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:create"))],
) -> ProductResponse:
    return await product_service.create_product(db, current_user.organization_id, data, current_user.id)


# ═══════════════════════════════════════════════════
# Stock In / Out (individual)
# ═══════════════════════════════════════════════════

@router.post("/stores/{store_id}/inventory/{item_id}/stock-in", response_model=TransactionResponse, status_code=201)
async def stock_in(
    store_id: UUID, item_id: UUID,
    data: TransactionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:create"))],
) -> TransactionResponse:
    await check_store_access(db, current_user, store_id)
    data.type = "stock_in"
    return await transaction_service.create_transaction(
        db, item_id, store_id, current_user.organization_id, data, current_user.id
    )


@router.post("/stores/{store_id}/inventory/{item_id}/stock-out", response_model=TransactionResponse, status_code=201)
async def stock_out(
    store_id: UUID, item_id: UUID,
    data: TransactionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:create"))],
) -> TransactionResponse:
    await check_store_access(db, current_user, store_id)
    data.type = "stock_out"
    return await transaction_service.create_transaction(
        db, item_id, store_id, current_user.organization_id, data, current_user.id
    )


@router.post("/stores/{store_id}/inventory/{item_id}/adjust", response_model=TransactionResponse, status_code=201)
async def adjust_stock(
    store_id: UUID, item_id: UUID,
    data: TransactionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:create"))],
) -> TransactionResponse:
    """Set actual quantity — creates adjustment transaction with difference."""
    await check_store_access(db, current_user, store_id)
    return await transaction_service.adjust_stock(
        db, item_id, store_id, current_user.organization_id,
        data.quantity, data.reason, current_user.id,
    )


# ═══════════════════════════════════════════════════
# Bulk Stock In / Out (multi-item pages)
# ═══════════════════════════════════════════════════

@router.post("/stores/{store_id}/inventory/bulk-stock-in", status_code=201)
async def bulk_stock_in(
    store_id: UUID,
    data: BulkTransactionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:create"))],
) -> list[TransactionResponse]:
    await check_store_access(db, current_user, store_id)
    data.type = "stock_in"
    return await transaction_service.bulk_transaction(
        db, store_id, current_user.organization_id, data, current_user.id
    )


@router.post("/stores/{store_id}/inventory/bulk-stock-out", status_code=201)
async def bulk_stock_out(
    store_id: UUID,
    data: BulkTransactionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:create"))],
) -> list[TransactionResponse]:
    await check_store_access(db, current_user, store_id)
    data.type = "stock_out"
    return await transaction_service.bulk_transaction(
        db, store_id, current_user.organization_id, data, current_user.id
    )


# ═══════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════

@router.post("/stores/{store_id}/inventory/audits", response_model=AuditDetailResponse, status_code=201)
async def start_audit(
    store_id: UUID,
    data: AuditCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:create"))],
) -> AuditDetailResponse:
    await check_store_access(db, current_user, store_id)
    return await audit_service.start_audit(
        db, store_id, current_user.organization_id, data, current_user.id
    )


@router.get("/stores/{store_id}/inventory/audits/{audit_id}", response_model=AuditDetailResponse)
async def get_audit(
    store_id: UUID, audit_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
) -> AuditDetailResponse:
    await check_store_access(db, current_user, store_id)
    return await audit_service.get_audit_detail(db, audit_id, store_id, current_user.organization_id)


@router.put("/stores/{store_id}/inventory/audits/{audit_id}/items", response_model=AuditDetailResponse)
async def update_audit_items(
    store_id: UUID, audit_id: UUID,
    data: AuditItemsBulkUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:create"))],
) -> AuditDetailResponse:
    await check_store_access(db, current_user, store_id)
    return await audit_service.update_audit_items(
        db, audit_id, store_id, current_user.organization_id, data
    )


@router.post("/stores/{store_id}/inventory/audits/{audit_id}/complete", response_model=AuditDetailResponse)
async def complete_audit(
    store_id: UUID, audit_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:create"))],
) -> AuditDetailResponse:
    await check_store_access(db, current_user, store_id)
    return await audit_service.complete_audit(
        db, audit_id, store_id, current_user.organization_id, current_user.id
    )
