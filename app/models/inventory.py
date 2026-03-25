"""Inventory management SQLAlchemy ORM model definitions.

Inventory management models for product catalog, store-level stock tracking,
stock transactions, and audit records.

Tables:
    - inventory_categories: Product categories (2-level, self-referencing)
    - inventory_products: Organization-wide product master catalog
    - store_inventory: Per-store stock tracking (quantity, min, frequency)
    - inventory_transactions: Stock in/out/adjustment history
    - inventory_audits: Audit session records
    - inventory_audit_items: Per-item audit results
    - inventory_audit_settings: Per-store audit frequency settings
"""

import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import (
    String, Boolean, DateTime, Integer, Text,
    ForeignKey, UniqueConstraint, Uuid, Numeric,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class InventoryCategory(Base):
    """Product category (2-level hierarchy via self-referencing parent_id).

    Top-level categories have parent_id=NULL.
    Subcategories reference their parent category.
    Extensible to 3-4 levels via the same parent_id pattern.

    Attributes:
        id: UUID primary key
        organization_id: FK to organizations
        name: Category name
        parent_id: FK to self (NULL = top-level, non-NULL = subcategory)
        sort_order: Display order (lower = first)
    """

    __tablename__ = "inventory_categories"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("inventory_categories.id", ondelete="CASCADE"), nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    organization = relationship("Organization", backref="inventory_categories")
    parent = relationship("InventoryCategory", remote_side="InventoryCategory.id", backref="children")


class InventorySubUnit(Base):
    """Sub-unit definition (e.g. box, pack, case).

    Organization-wide managed list of sub-units. Products reference these
    instead of free-text strings.

    Attributes:
        id: UUID primary key
        organization_id: FK to organizations
        name: Sub-unit display name (e.g. "Box", "Pack")
        code: Lowercase identifier (e.g. "box", "pack") — unique within org
    """

    __tablename__ = "inventory_sub_units"
    __table_args__ = (
        UniqueConstraint("organization_id", "code", name="uq_inventory_sub_units_org_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    organization = relationship("Organization", backref="inventory_sub_units")


class InventoryProduct(Base):
    """Organization-wide product master catalog.

    Products are shared across all stores in an organization.
    Base unit is always ea (piece). Sub-unit defines optional bulk packaging.

    Attributes:
        id: UUID primary key
        organization_id: FK to organizations
        name: Product name (include spec in name, e.g. "Whole Milk (1L)")
        code: Unique product code within org (auto-generated or manual)
        barcode: Barcode string (Phase 2)
        category_id: FK to inventory_categories (top-level)
        subcategory_id: FK to inventory_categories (sub-level)
        sub_unit: Optional sub-unit name (box, pack, case, bag, etc.)
        sub_unit_ratio: How many ea per sub-unit (required if sub_unit is set)
        image_url: Relative path key (resolved via storage_service.resolve_url)
        description: Product description (displayed in list and detail views)
        is_active: Soft delete flag (false = deactivated)
    """

    __tablename__ = "inventory_products"
    __table_args__ = (
        UniqueConstraint("organization_id", "code", name="uq_inventory_products_org_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    barcode: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    category_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("inventory_categories.id", ondelete="SET NULL"), nullable=True
    )
    subcategory_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("inventory_categories.id", ondelete="SET NULL"), nullable=True
    )
    sub_unit: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    sub_unit_ratio: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    organization = relationship("Organization", backref="inventory_products")
    category = relationship("InventoryCategory", foreign_keys=[category_id])
    subcategory = relationship("InventoryCategory", foreign_keys=[subcategory_id])


class StoreInventory(Base):
    """Per-store stock tracking for a product.

    Links a product from the master catalog to a specific store with
    store-specific settings (min quantity, audit frequency).

    Attributes:
        id: UUID primary key
        store_id: FK to stores
        product_id: FK to inventory_products
        current_quantity: Current stock in ea (can be negative)
        min_quantity: Alert threshold (notification when current <= min)
        is_frequent: Needs frequent stock checks
        last_audited_at: Last audit timestamp for this item
        is_active: Soft delete flag
    """

    __tablename__ = "store_inventory"
    __table_args__ = (
        UniqueConstraint("store_id", "product_id", name="uq_store_inventory_store_product"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("inventory_products.id", ondelete="CASCADE"), nullable=False
    )
    current_quantity: Mapped[int] = mapped_column(Integer, default=0)
    min_quantity: Mapped[int] = mapped_column(Integer, default=0)
    is_frequent: Mapped[bool] = mapped_column(Boolean, default=False)
    last_audited_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    store = relationship("Store", backref="store_inventory")
    product = relationship("InventoryProduct", backref="store_inventory_items")


class InventoryTransaction(Base):
    """Stock movement history (in/out/adjustment).

    Every quantity change creates a transaction record for full audit trail.
    Quantities are always in ea. Sub-unit conversion happens before recording.

    Attributes:
        id: UUID primary key
        store_inventory_id: FK to store_inventory
        type: stock_in / stock_out / adjustment
        quantity: Change amount (positive for in, negative for out/adjustment)
        before_quantity: Stock before this transaction
        after_quantity: Stock after this transaction
        reason: Free-text reason (Phase 2: codified)
        unit_price: Per-unit cost (Phase 2, nullable)
        audit_id: FK to inventory_audits (for adjustment type)
        created_by: FK to users (who performed the action)
    """

    __tablename__ = "inventory_transactions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    store_inventory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("store_inventory.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    before_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    after_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    unit_price: Mapped[Optional[int]] = mapped_column(Numeric(10, 2), nullable=True)
    audit_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("inventory_audits.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    store_inventory = relationship("StoreInventory", backref="transactions")
    audit = relationship("InventoryAudit", backref="transactions")
    created_by_user = relationship("User", foreign_keys=[created_by])


class InventoryAudit(Base):
    """Audit session record.

    Tracks a full inventory audit for a store. Contains individual item
    results via inventory_audit_items.

    Attributes:
        id: UUID primary key
        store_id: FK to stores
        audited_by: FK to users (who performed the audit)
        status: in_progress / completed
        started_at: When audit began
        completed_at: When audit was finalized (nullable if in_progress)
        note: Optional notes
    """

    __tablename__ = "inventory_audits"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False
    )
    audited_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="in_progress")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    store = relationship("Store", backref="inventory_audits")
    auditor = relationship("User", foreign_keys=[audited_by])


class InventoryAuditItem(Base):
    """Per-item audit result within an audit session.

    Records the system vs actual quantity for each product during an audit.
    Difference is computed as actual - system.

    Attributes:
        id: UUID primary key
        audit_id: FK to inventory_audits
        store_inventory_id: FK to store_inventory
        system_quantity: System quantity at audit start
        actual_quantity: Actual counted quantity (entered by auditor)
        difference: actual - system (auto-computed)
    """

    __tablename__ = "inventory_audit_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    audit_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("inventory_audits.id", ondelete="CASCADE"), nullable=False
    )
    store_inventory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("store_inventory.id", ondelete="CASCADE"), nullable=False
    )
    system_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    difference: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    audit = relationship("InventoryAudit", backref="items")
    store_inventory = relationship("StoreInventory")


class InventoryAuditSetting(Base):
    """Per-store audit frequency setting.

    Attributes:
        id: UUID primary key
        store_id: FK to stores (one per store)
        frequency: daily / weekly / custom
        day_of_week: 0=Mon..6=Sun (for weekly)
    """

    __tablename__ = "inventory_audit_settings"
    __table_args__ = (
        UniqueConstraint("store_id", name="uq_inventory_audit_settings_store"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False
    )
    frequency: Mapped[str] = mapped_column(String(20), default="daily")
    day_of_week: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    store = relationship("Store", backref="inventory_audit_settings")
