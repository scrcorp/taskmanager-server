"""schedule_requests에 original 필드, created_by, rejection_reason 추가

Revision ID: 50ae06dae166
Revises: 4c5d5bfe64d3
Create Date: 2026-03-12 15:40:38.803530

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '50ae06dae166'
down_revision: Union[str, None] = '4c5d5bfe64d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('schedule_requests', sa.Column('original_preferred_start_time', sa.Time(), nullable=True))
    op.add_column('schedule_requests', sa.Column('original_preferred_end_time', sa.Time(), nullable=True))
    op.add_column('schedule_requests', sa.Column('original_work_role_id', sa.Uuid(), nullable=True))
    op.add_column('schedule_requests', sa.Column('original_user_id', sa.Uuid(), nullable=True))
    op.add_column('schedule_requests', sa.Column('original_work_date', sa.Date(), nullable=True))
    op.add_column('schedule_requests', sa.Column('created_by', sa.Uuid(), nullable=True))
    op.add_column('schedule_requests', sa.Column('rejection_reason', sa.Text(), nullable=True))
    op.create_foreign_key('fk_schedule_requests_original_work_role_id', 'schedule_requests', 'store_work_roles', ['original_work_role_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('fk_schedule_requests_created_by', 'schedule_requests', 'users', ['created_by'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('fk_schedule_requests_original_user_id', 'schedule_requests', 'users', ['original_user_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_schedule_requests_original_user_id', 'schedule_requests', type_='foreignkey')
    op.drop_constraint('fk_schedule_requests_created_by', 'schedule_requests', type_='foreignkey')
    op.drop_constraint('fk_schedule_requests_original_work_role_id', 'schedule_requests', type_='foreignkey')
    op.drop_column('schedule_requests', 'rejection_reason')
    op.drop_column('schedule_requests', 'created_by')
    op.drop_column('schedule_requests', 'original_work_date')
    op.drop_column('schedule_requests', 'original_user_id')
    op.drop_column('schedule_requests', 'original_work_role_id')
    op.drop_column('schedule_requests', 'original_preferred_end_time')
    op.drop_column('schedule_requests', 'original_preferred_start_time')
