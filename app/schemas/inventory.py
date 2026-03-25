"""Inventory Pydantic request/response schema definitions.

Covers categories, products, store inventory, transactions, and audits.
"""

from pydantic import BaseModel


# === Category ===

class CategoryCreate(BaseModel):
    name: str
    parent_id: str | None = None
    sort_order: int = 0


class CategoryUpdate(BaseModel):
    name: str | None = None
    sort_order: int | None = None


class CategoryResponse(BaseModel):
    id: str
    organization_id: str
    name: str
    parent_id: str | None = None
    sort_order: int
    product_count: int = 0


class CategoryTreeResponse(BaseModel):
    id: str
    organization_id: str
    name: str
    sort_order: int
    product_count: int = 0
    children: list["CategoryTreeResponse"] = []


# === Product ===

class ProductStoreEntry(BaseModel):
    """Used when creating a product with optional store assignment."""
    store_id: str
    min_quantity: int = 0
    initial_quantity: int = 0
    is_frequent: bool = False


class ProductCreate(BaseModel):
    name: str
    code: str | None = None  # None = auto-generate
    category_id: str | None = None
    subcategory_id: str | None = None
    sub_unit: str | None = None
    sub_unit_ratio: int | None = None
    image_url: str | None = None
    description: str | None = None
    stores: list[ProductStoreEntry] = []  # optional store assignment on creation


class ProductUpdate(BaseModel):
    name: str | None = None
    code: str | None = None
    category_id: str | None = None
    subcategory_id: str | None = None
    sub_unit: str | None = None
    sub_unit_ratio: int | None = None
    image_url: str | None = None
    description: str | None = None


class ProductResponse(BaseModel):
    id: str
    organization_id: str
    name: str
    code: str
    barcode: str | None = None
    category_id: str | None = None
    category_name: str | None = None
    subcategory_id: str | None = None
    subcategory_name: str | None = None
    sub_unit: str | None = None
    sub_unit_ratio: int | None = None
    image_url: str | None = None
    description: str | None = None
    is_active: bool
    store_count: int = 0


class ProductDetailResponse(ProductResponse):
    stores: list["StoreInventoryBrief"] = []


class StoreInventoryBrief(BaseModel):
    """Brief store inventory info shown in product detail."""
    store_id: str
    store_name: str
    current_quantity: int
    min_quantity: int
    is_frequent: bool


# === Store Inventory ===

class StoreInventoryAddItem(BaseModel):
    """Single item for bulk add to store."""
    product_id: str
    min_quantity: int = 0
    initial_quantity: int = 0
    is_frequent: bool = False


class StoreInventoryBulkAdd(BaseModel):
    """Bulk add products to store."""
    items: list[StoreInventoryAddItem]


class StoreInventoryUpdate(BaseModel):
    min_quantity: int | None = None
    is_frequent: bool | None = None


class StoreInventoryResponse(BaseModel):
    id: str
    store_id: str
    product_id: str
    product_name: str
    product_code: str
    category_name: str | None = None
    sub_unit: str | None = None
    sub_unit_ratio: int | None = None
    image_url: str | None = None
    description: str | None = None
    current_quantity: int
    min_quantity: int
    is_frequent: bool
    last_audited_at: str | None = None
    is_active: bool
    status: str  # normal / low / out


# === Transaction ===

class TransactionCreate(BaseModel):
    type: str  # stock_in / stock_out
    quantity: int
    reason: str | None = None


class BulkTransactionItem(BaseModel):
    store_inventory_id: str
    quantity: int
    reason: str | None = None


class BulkTransactionCreate(BaseModel):
    """Bulk stock in or out."""
    type: str  # stock_in / stock_out
    items: list[BulkTransactionItem]
    date: str | None = None  # optional custom date (ISO)
    reason: str | None = None  # shared reason for all items


class TransactionResponse(BaseModel):
    id: str
    store_inventory_id: str
    product_name: str
    product_code: str
    type: str
    quantity: int
    before_quantity: int
    after_quantity: int
    reason: str | None = None
    created_by: str
    created_by_name: str | None = None
    created_at: str


# === Audit ===

class AuditCreate(BaseModel):
    """Start a new audit (no body needed, just POST)."""
    note: str | None = None


class AuditItemUpdate(BaseModel):
    store_inventory_id: str
    actual_quantity: int


class AuditItemsBulkUpdate(BaseModel):
    items: list[AuditItemUpdate]


class AuditResponse(BaseModel):
    id: str
    store_id: str
    audited_by: str
    auditor_name: str | None = None
    status: str
    started_at: str
    completed_at: str | None = None
    note: str | None = None
    items_count: int = 0
    discrepancies: int = 0


class AuditItemResponse(BaseModel):
    id: str
    store_inventory_id: str
    product_name: str
    product_code: str
    system_quantity: int
    actual_quantity: int
    difference: int
    is_frequent: bool = False


class AuditDetailResponse(AuditResponse):
    items: list[AuditItemResponse] = []


# === Audit Settings ===

class AuditSettingUpdate(BaseModel):
    frequency: str = "daily"
    day_of_week: int | None = None


class AuditSettingResponse(BaseModel):
    id: str
    store_id: str
    frequency: str
    day_of_week: int | None = None
