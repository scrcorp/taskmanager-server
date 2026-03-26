"""Admin inventory router — Categories, Products, Store Inventory, Transactions, Audits.

All inventory admin endpoints consolidated in one router file.
"""

import io
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.inventory import (
    CategoryCreate, CategoryUpdate, CategoryTreeResponse,
    ProductCreate, ProductUpdate, ProductResponse, ProductDetailResponse,
    StoreInventoryBulkAdd, StoreInventoryUpdate, StoreInventoryResponse,
    TransactionCreate, TransactionResponse, BulkTransactionCreate,
    AuditResponse, AuditDetailResponse,
    AuditSettingUpdate, AuditSettingResponse,
)
from app.services.inventory_service import (
    category_service, sub_unit_service, product_service,
    store_inventory_service,
    transaction_service, audit_service, audit_setting_service,
)

router = APIRouter()


# ═══════════════════════════════════════════════════
# Categories
# ═══════════════════════════════════════════════════

@router.get("/inventory/categories", response_model=list[CategoryTreeResponse])
async def list_categories(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
) -> list[CategoryTreeResponse]:
    return await category_service.list_tree(db, current_user.organization_id)


@router.post("/inventory/categories", status_code=201)
async def create_category(
    data: CategoryCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:create"))],
):
    return await category_service.create(db, current_user.organization_id, data)


@router.put("/inventory/categories/{category_id}")
async def update_category(
    category_id: UUID,
    data: CategoryUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:update"))],
):
    return await category_service.update(db, category_id, current_user.organization_id, data)


@router.delete("/inventory/categories/{category_id}", status_code=204)
async def delete_category(
    category_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:delete"))],
) -> None:
    await category_service.delete(db, category_id, current_user.organization_id)


# ═══════════════════════════════════════════════════
# Sub Units
# ═══════════════════════════════════════════════════

@router.get("/inventory/sub-units")
async def list_sub_units(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
) -> list:
    return await sub_unit_service.list_sub_units(db, current_user.organization_id)


@router.post("/inventory/sub-units", status_code=201)
async def create_sub_unit(
    data: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:create"))],
) -> dict:
    return await sub_unit_service.create(
        db, current_user.organization_id, data.get("name", ""), data.get("code"),
    )


@router.put("/inventory/sub-units/{sub_unit_id}")
async def update_sub_unit(
    sub_unit_id: UUID,
    data: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:update"))],
) -> dict:
    return await sub_unit_service.update(
        db, sub_unit_id, current_user.organization_id, data.get("name", ""),
    )


@router.delete("/inventory/sub-units/{sub_unit_id}", status_code=204)
async def delete_sub_unit(
    sub_unit_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:delete"))],
) -> None:
    await sub_unit_service.delete(db, sub_unit_id, current_user.organization_id)


# ═══════════════════════════════════════════════════
# Products
# ═══════════════════════════════════════════════════

@router.get("/inventory/products")
async def list_products(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
    keyword: str | None = None,
    search_field: str | None = None,
    category_id: str | None = None,
    is_active: bool | None = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    items, total = await product_service.list_products(
        db, current_user.organization_id, keyword, search_field, category_id, is_active, page, per_page
    )
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.post("/inventory/products", response_model=ProductResponse, status_code=201)
async def create_product(
    data: ProductCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:create"))],
) -> ProductResponse:
    return await product_service.create_product(db, current_user.organization_id, data, current_user.id)


@router.get("/inventory/products/generate-code")
async def preview_product_code(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
) -> dict:
    """Preview what auto-generated product code would be."""
    code = await product_service.preview_code(db, current_user.organization_id)
    return {"code": code}


from pathlib import Path
from fastapi.responses import FileResponse as _FileResponse
from app.config import settings

_STATIC_DIR = Path(__file__).resolve().parents[3] / "static"


@router.get("/inventory/products/excel-template")
async def download_excel_template(
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
) -> _FileResponse:
    """Download static Excel template for bulk product import."""
    from fastapi import HTTPException
    filename = settings.INVENTORY_TEMPLATE_EXCEL
    template_path = _STATIC_DIR / filename
    if not template_path.exists():
        raise HTTPException(status_code=404, detail=f"Template file not found: {filename}")
    return _FileResponse(
        path=template_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


def _parse_excel_file(content: bytes) -> dict:
    """Parse Excel file and return parsed rows or error dict."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content))
    ws = wb.active

    all_rows = list(ws.iter_rows(values_only=True))
    if not all_rows:
        return {"error": "Empty file"}

    header_row = all_rows[0]
    headers = [str(h or "").strip().lower().replace(" *", "").replace("*", "") for h in header_row]

    data_start_idx = None
    data_end_idx = None
    for i, row in enumerate(all_rows):
        first_cell = str(row[0] or "").strip().upper() if row and row[0] else ""
        if first_cell == "--- DATA START ---":
            if data_start_idx is None:
                data_start_idx = i
        elif first_cell == "--- DATA END ---":
            if data_end_idx is None:
                data_end_idx = i

    if data_start_idx is None:
        return {"error": "Missing '--- DATA START ---' marker row. Please use the provided template."}

    end = data_end_idx if data_end_idx is not None else len(all_rows)
    parsed_rows = []
    for row in all_rows[data_start_idx + 1 : end]:
        row_dict = {}
        for i, val in enumerate(row):
            if i < len(headers):
                key = headers[i]
                str_val = str(val).strip() if val is not None else ""
                if str_val in ("-", "", "None"):
                    str_val = ""
                row_dict[key] = str_val
        if not row_dict.get("name"):
            continue
        name_val = row_dict["name"]
        if len(name_val) > 100:
            continue
        if any(kw in name_val.lower() for kw in ["data start", "data end", "required", "optional", "comma-separated", "auto-generated", "product info", "store link"]):
            continue
        parsed_rows.append(row_dict)

    if not parsed_rows:
        return {"error": "No data rows found between DATA START and DATA END markers."}

    return {"rows": parsed_rows}


@router.post("/inventory/products/preview-import")
async def preview_import(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("inventory:create")),
) -> dict:
    """Preview Excel import — parse and check duplicates without creating anything."""
    content = await file.read()
    parse_result = _parse_excel_file(content)
    if "error" in parse_result:
        return parse_result

    parsed_rows = parse_result["rows"]
    preview_items = []
    for i, row in enumerate(parsed_rows):
        name = (row.get("name") or "").strip()
        code = (row.get("code") or "").strip() or None
        store_codes = (row.get("store_code") or "").strip()

        # Check existing product by code
        existing = None
        if code:
            from app.repositories.inventory_repository import product_repository
            existing_check = await product_repository.exists(db, {
                "organization_id": current_user.organization_id, "code": code,
            })
            if existing_check:
                existing = code

        # Check name duplicate
        from app.models.inventory import InventoryProduct
        from sqlalchemy import func as _func, select as _sel
        name_check = _sel(InventoryProduct).where(
            InventoryProduct.organization_id == current_user.organization_id,
            _func.lower(InventoryProduct.name) == name.lower(),
            InventoryProduct.is_active == True,
        ).limit(1)
        name_dup = (await db.execute(name_check)).scalar_one_or_none()

        preview_items.append({
            "row": i + 1,
            "name": name,
            "code": code,
            "category": (row.get("category") or "").strip(),
            "store_codes": store_codes,
            "existing_code": existing,
            "duplicate_name": name_dup.code if name_dup else None,
            "action": "link" if existing else "create",
        })

    return {"items": preview_items, "total": len(preview_items)}


@router.post("/inventory/products/import")
async def import_products_from_excel(
    file: UploadFile = File(...),
    selected_rows: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("inventory:create")),
) -> dict:
    """Import products from uploaded Excel file.

    selected_rows: comma-separated 1-based row numbers from preview.
    If empty, imports all rows.
    """
    content = await file.read()
    parse_result = _parse_excel_file(content)
    if "error" in parse_result:
        return parse_result

    all_parsed_rows = parse_result["rows"]
    total_parsed = len(all_parsed_rows)

    # Filter to selected rows only (1-based index from preview)
    skipped_by_user = 0
    parsed_rows = all_parsed_rows
    if selected_rows.strip():
        import json
        try:
            selected = set(json.loads(selected_rows))
        except (json.JSONDecodeError, TypeError):
            selected = set(int(x.strip()) for x in selected_rows.split(",") if x.strip().isdigit())
        if selected:
            parsed_rows = [row for i, row in enumerate(all_parsed_rows) if (i + 1) in selected]
            skipped_by_user = total_parsed - len(parsed_rows)

    if not parsed_rows:
        return {"error": "No rows selected for import."}

    # Dry-run validation
    validation_errors = []
    for i, row in enumerate(parsed_rows):
        name = row.get("name", "").strip()
        if not name:
            validation_errors.append(f"Row {i+1}: name is empty")
        if len(name) < 2:
            validation_errors.append(f"Row {i+1}: name too short ('{name}')")

    if validation_errors:
        return {"error": "Validation failed. Nothing was imported.", "validation_errors": validation_errors, "rows_parsed": len(parsed_rows)}

    result = await product_service.import_from_excel(
        db, current_user.organization_id, parsed_rows, current_user.id
    )
    result["rows_parsed"] = total_parsed
    # Add user-skipped count to existing skipped list
    existing_skipped = result.get("skipped", [])
    if isinstance(existing_skipped, list):
        if skipped_by_user > 0:
            existing_skipped.append(f"{skipped_by_user} item(s) unchecked in preview")
        result["skipped"] = existing_skipped
    else:
        result["skipped"] = (existing_skipped or 0) + skipped_by_user
    return result


# ── Product CRUD with path params (MUST be after /generate-code, /excel-template, /import) ──

@router.get("/inventory/products/{product_id}", response_model=ProductDetailResponse)
async def get_product(
    product_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
) -> ProductDetailResponse:
    return await product_service.get_product(db, product_id, current_user.organization_id)


@router.put("/inventory/products/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: UUID,
    data: ProductUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:update"))],
) -> ProductResponse:
    return await product_service.update_product(db, product_id, current_user.organization_id, data)


@router.delete("/inventory/products/{product_id}", status_code=204)
async def deactivate_product(
    product_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:delete"))],
) -> None:
    await product_service.deactivate_product(db, product_id, current_user.organization_id)


@router.post("/inventory/products/{product_id}/activate", response_model=ProductResponse)
async def activate_product(
    product_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:update"))],
) -> ProductResponse:
    """Reactivate a deactivated product."""
    return await product_service.activate_product(db, product_id, current_user.organization_id)


@router.post("/inventory/products/{product_id}/delete", status_code=204)
async def permanently_delete_product(
    product_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:delete"))],
) -> None:
    """Permanently delete a product and ALL related data (store inventory, transactions, audits).
    This action cannot be undone. POST instead of DELETE to avoid accidental calls."""
    await product_service.hard_delete_product(db, product_id, current_user.organization_id)


# ═══════════════════════════════════════════════════
# Store Inventory
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
async def bulk_add_store_inventory(
    store_id: UUID,
    data: StoreInventoryBulkAdd,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:create"))],
) -> list[StoreInventoryResponse]:
    await check_store_access(db, current_user, store_id)
    return await store_inventory_service.bulk_add(
        db, store_id, current_user.organization_id, data, current_user.id
    )


@router.put("/stores/{store_id}/inventory/{item_id}", response_model=StoreInventoryResponse)
async def update_store_inventory_item(
    store_id: UUID, item_id: UUID,
    data: StoreInventoryUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:update"))],
) -> StoreInventoryResponse:
    await check_store_access(db, current_user, store_id)
    return await store_inventory_service.update_item(
        db, item_id, store_id, current_user.organization_id, data
    )


@router.delete("/stores/{store_id}/inventory/{item_id}", status_code=204)
async def deactivate_store_inventory_item(
    store_id: UUID, item_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:delete"))],
) -> None:
    await check_store_access(db, current_user, store_id)
    await store_inventory_service.deactivate_item(
        db, item_id, store_id, current_user.organization_id
    )


# ═══════════════════════════════════════════════════
# Transactions
# ═══════════════════════════════════════════════════

@router.get("/stores/{store_id}/inventory/transactions")
async def list_store_transactions(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
    product_id: str | None = None,
    type: str | None = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    await check_store_access(db, current_user, store_id)
    items, total = await transaction_service.list_transactions(
        db, store_id, current_user.organization_id, product_id, type, page, per_page
    )
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.post("/stores/{store_id}/inventory/{item_id}/transactions", response_model=TransactionResponse, status_code=201)
async def create_transaction(
    store_id: UUID, item_id: UUID,
    data: TransactionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:create"))],
) -> TransactionResponse:
    await check_store_access(db, current_user, store_id)
    return await transaction_service.create_transaction(
        db, item_id, store_id, current_user.organization_id, data, current_user.id
    )


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
# Audits
# ═══════════════════════════════════════════════════

@router.get("/stores/{store_id}/inventory/audits")
async def list_audits(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
    page: int = 1,
    per_page: int = 20,
) -> dict:
    await check_store_access(db, current_user, store_id)
    items, total = await audit_service.list_audits(
        db, store_id, current_user.organization_id, page, per_page
    )
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/stores/{store_id}/inventory/audits/{audit_id}", response_model=AuditDetailResponse)
async def get_audit(
    store_id: UUID, audit_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
) -> AuditDetailResponse:
    await check_store_access(db, current_user, store_id)
    return await audit_service.get_audit_detail(db, audit_id, store_id, current_user.organization_id)


# ═══════════════════════════════════════════════════
# Audit Settings
# ═══════════════════════════════════════════════════

@router.get("/stores/{store_id}/inventory/audit-settings", response_model=AuditSettingResponse)
async def get_audit_settings(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:read"))],
) -> AuditSettingResponse:
    await check_store_access(db, current_user, store_id)
    return await audit_setting_service.get_setting(db, store_id, current_user.organization_id)


@router.put("/stores/{store_id}/inventory/audit-settings", response_model=AuditSettingResponse)
async def update_audit_settings(
    store_id: UUID,
    data: AuditSettingUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("inventory:update"))],
) -> AuditSettingResponse:
    await check_store_access(db, current_user, store_id)
    return await audit_setting_service.update_setting(db, store_id, current_user.organization_id, data)
