"""rename brand to store

Revision ID: b1c2d3e4f5g6
Revises: a1b2c3d4e5f6
Create Date: 2026-02-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5g6"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. Rename tables ---
    op.rename_table("brands", "stores")
    op.rename_table("user_brands", "user_stores")

    # --- 2. Rename brand_id columns to store_id ---
    # user_stores (formerly user_brands)
    op.alter_column("user_stores", "brand_id", new_column_name="store_id")
    # shifts
    op.alter_column("shifts", "brand_id", new_column_name="store_id")
    # positions
    op.alter_column("positions", "brand_id", new_column_name="store_id")
    # checklist_templates
    op.alter_column("checklist_templates", "brand_id", new_column_name="store_id")
    # work_assignments
    op.alter_column("work_assignments", "brand_id", new_column_name="store_id")
    # additional_tasks
    op.alter_column("additional_tasks", "brand_id", new_column_name="store_id")
    # announcements
    op.alter_column("announcements", "brand_id", new_column_name="store_id")

    # --- 3. Rename indexes ---
    op.execute("ALTER INDEX IF EXISTS idx_brands_org RENAME TO idx_stores_org")
    op.execute("ALTER INDEX IF EXISTS idx_user_brands_user RENAME TO idx_user_stores_user")
    op.execute("ALTER INDEX IF EXISTS idx_user_brands_brand RENAME TO idx_user_stores_store")
    op.execute("ALTER INDEX IF EXISTS idx_shifts_brand RENAME TO idx_shifts_store")
    op.execute("ALTER INDEX IF EXISTS idx_positions_brand RENAME TO idx_positions_store")
    op.execute("ALTER INDEX IF EXISTS idx_work_assignments_brand_date RENAME TO idx_work_assignments_store_date")
    op.execute("ALTER INDEX IF EXISTS idx_announcements_brand RENAME TO idx_announcements_store")

    # --- 4. Rename constraints ---
    op.execute("ALTER TABLE user_stores RENAME CONSTRAINT uq_user_brand TO uq_user_store")
    op.execute("ALTER TABLE shifts RENAME CONSTRAINT uq_shift_brand_name TO uq_shift_store_name")
    op.execute("ALTER TABLE positions RENAME CONSTRAINT uq_position_brand_name TO uq_position_store_name")
    op.execute("ALTER TABLE checklist_templates RENAME CONSTRAINT uq_template_brand_shift_position TO uq_template_store_shift_position")


def downgrade() -> None:
    # --- 4. Rename constraints back ---
    op.execute("ALTER TABLE checklist_templates RENAME CONSTRAINT uq_template_store_shift_position TO uq_template_brand_shift_position")
    op.execute("ALTER TABLE positions RENAME CONSTRAINT uq_position_store_name TO uq_position_brand_name")
    op.execute("ALTER TABLE shifts RENAME CONSTRAINT uq_shift_store_name TO uq_shift_brand_name")
    op.execute("ALTER TABLE user_stores RENAME CONSTRAINT uq_user_store TO uq_user_brand")

    # --- 3. Rename indexes back ---
    op.execute("ALTER INDEX IF EXISTS idx_announcements_store RENAME TO idx_announcements_brand")
    op.execute("ALTER INDEX IF EXISTS idx_work_assignments_store_date RENAME TO idx_work_assignments_brand_date")
    op.execute("ALTER INDEX IF EXISTS idx_positions_store RENAME TO idx_positions_brand")
    op.execute("ALTER INDEX IF EXISTS idx_shifts_store RENAME TO idx_shifts_brand")
    op.execute("ALTER INDEX IF EXISTS idx_user_stores_store RENAME TO idx_user_brands_brand")
    op.execute("ALTER INDEX IF EXISTS idx_user_stores_user RENAME TO idx_user_brands_user")
    op.execute("ALTER INDEX IF EXISTS idx_stores_org RENAME TO idx_brands_org")

    # --- 2. Rename store_id columns back to brand_id ---
    op.alter_column("announcements", "store_id", new_column_name="brand_id")
    op.alter_column("additional_tasks", "store_id", new_column_name="brand_id")
    op.alter_column("work_assignments", "store_id", new_column_name="brand_id")
    op.alter_column("checklist_templates", "store_id", new_column_name="brand_id")
    op.alter_column("positions", "store_id", new_column_name="brand_id")
    op.alter_column("shifts", "store_id", new_column_name="brand_id")
    op.alter_column("user_stores", "store_id", new_column_name="brand_id")

    # --- 1. Rename tables back ---
    op.rename_table("user_stores", "user_brands")
    op.rename_table("stores", "brands")
