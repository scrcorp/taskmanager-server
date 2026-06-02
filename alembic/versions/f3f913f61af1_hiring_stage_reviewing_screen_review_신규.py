"""hiring stage reviewing->screen + review 신규

Revision ID: f3f913f61af1
Revises: 31a441b424d1
Create Date: 2026-06-01 15:47:25.272766

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3f913f61af1'
down_revision: Union[str, None] = '31a441b424d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_IDX = "uq_active_application_per_store"


def upgrade() -> None:
    # 1) 기존 'reviewing'(서류검토) → 'screen' 으로 데이터 rename. ('review'(검수)는 신규 — 기존 행 없음)
    op.execute("UPDATE applications SET stage = 'screen' WHERE stage = 'reviewing'")
    # 2) 활성 application 부분 unique 인덱스 predicate 갱신 (new/screen/interview/review)
    op.drop_index(_IDX, table_name="applications")
    op.create_index(
        _IDX,
        "applications",
        ["candidate_id", "store_id"],
        unique=True,
        postgresql_where=sa.text("stage IN ('new','screen','interview','review')"),
    )


def downgrade() -> None:
    # screen/review 둘 다 옛 단일 'reviewing' 으로 되돌림
    op.execute("UPDATE applications SET stage = 'reviewing' WHERE stage IN ('screen', 'review')")
    op.drop_index(_IDX, table_name="applications")
    op.create_index(
        _IDX,
        "applications",
        ["candidate_id", "store_id"],
        unique=True,
        postgresql_where=sa.text("stage IN ('new','reviewing','interview')"),
    )
