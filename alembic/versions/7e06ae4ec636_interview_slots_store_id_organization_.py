"""interview_slots store_id -> organization_id (org 통합)

Revision ID: 7e06ae4ec636
Revises: b6a07c07be85
Create Date: 2026-06-01 16:47:31.590411

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7e06ae4ec636'
down_revision: Union[str, None] = 'b6a07c07be85'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # interview_slots: store_id → organization_id (org 통합)
    op.add_column("interview_slots", sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.execute(
        "UPDATE interview_slots s SET organization_id = "
        "(SELECT st.organization_id FROM stores st WHERE st.id = s.store_id)"
    )
    op.alter_column("interview_slots", "organization_id", nullable=False)
    op.create_foreign_key(
        "fk_interview_slots_org", "interview_slots", "organizations",
        ["organization_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index(op.f("ix_interview_slots_organization_id"), "interview_slots", ["organization_id"])
    # 옛 store 기반 unique/인덱스/컬럼 제거 후 org 기반 unique 재생성 (이름 동일하므로 drop 먼저)
    op.drop_constraint("uq_interview_slot_time", "interview_slots", type_="unique")
    op.drop_index("ix_interview_slots_store_id", table_name="interview_slots")
    op.drop_column("interview_slots", "store_id")
    op.create_unique_constraint(
        "uq_interview_slot_time", "interview_slots", ["organization_id", "slot_date", "start_time"]
    )


def downgrade() -> None:
    op.add_column("interview_slots", sa.Column("store_id", sa.Uuid(), nullable=True))
    # org 의 임의 매장으로 backfill (best-effort)
    op.execute(
        "UPDATE interview_slots s SET store_id = "
        "(SELECT st.id FROM stores st WHERE st.organization_id = s.organization_id LIMIT 1)"
    )
    op.create_index("ix_interview_slots_store_id", "interview_slots", ["store_id"])
    op.create_foreign_key(
        "interview_slots_store_id_fkey", "interview_slots", "stores",
        ["store_id"], ["id"], ondelete="CASCADE",
    )
    op.drop_constraint("uq_interview_slot_time", "interview_slots", type_="unique")
    op.create_unique_constraint(
        "uq_interview_slot_time", "interview_slots", ["store_id", "slot_date", "start_time"]
    )
    op.drop_index(op.f("ix_interview_slots_organization_id"), table_name="interview_slots")
    op.drop_constraint("fk_interview_slots_org", "interview_slots", type_="foreignkey")
    op.drop_column("interview_slots", "organization_id")
