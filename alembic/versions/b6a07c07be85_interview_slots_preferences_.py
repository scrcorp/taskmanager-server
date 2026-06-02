"""interview_slots + preferences + applications interview fields

Revision ID: b6a07c07be85
Revises: f3f913f61af1
Create Date: 2026-06-01 16:02:10.540577

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b6a07c07be85'
down_revision: Union[str, None] = 'f3f913f61af1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 인터뷰 스케줄링 테이블 + applications 필드만. (autogenerate 가 잡은 무관 drift 는 제거함)
    op.create_table(
        'interview_slots',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('store_id', sa.Uuid(), nullable=False),
        sa.Column('slot_date', sa.Date(), nullable=False),
        sa.Column('start_time', sa.Time(), nullable=False),
        sa.Column('end_time', sa.Time(), nullable=False),
        sa.Column('created_by_user_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('store_id', 'slot_date', 'start_time', name='uq_interview_slot_time'),
    )
    op.create_index(op.f('ix_interview_slots_store_id'), 'interview_slots', ['store_id'], unique=False)
    op.create_table(
        'interview_slot_preferences',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('application_id', sa.Uuid(), nullable=False),
        sa.Column('slot_id', sa.Uuid(), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['slot_id'], ['interview_slots.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('application_id', 'slot_id', name='uq_pref_application_slot'),
    )
    op.create_index(op.f('ix_interview_slot_preferences_application_id'), 'interview_slot_preferences', ['application_id'], unique=False)
    op.create_index(op.f('ix_interview_slot_preferences_slot_id'), 'interview_slot_preferences', ['slot_id'], unique=False)
    op.create_index('ix_pref_slot', 'interview_slot_preferences', ['slot_id'], unique=False)

    op.add_column('applications', sa.Column('confirmed_slot_id', sa.Uuid(), nullable=True))
    op.add_column('applications', sa.Column('interviewer_id', sa.Uuid(), nullable=True))
    op.add_column('applications', sa.Column('interview_token', sa.String(length=64), nullable=True))
    op.create_foreign_key(
        'fk_applications_confirmed_slot', 'applications', 'interview_slots',
        ['confirmed_slot_id'], ['id'], ondelete='SET NULL',
    )
    op.create_foreign_key(
        'fk_applications_interviewer', 'applications', 'users',
        ['interviewer_id'], ['id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_applications_interviewer', 'applications', type_='foreignkey')
    op.drop_constraint('fk_applications_confirmed_slot', 'applications', type_='foreignkey')
    op.drop_column('applications', 'interview_token')
    op.drop_column('applications', 'interviewer_id')
    op.drop_column('applications', 'confirmed_slot_id')
    op.drop_index('ix_pref_slot', table_name='interview_slot_preferences')
    op.drop_index(op.f('ix_interview_slot_preferences_slot_id'), table_name='interview_slot_preferences')
    op.drop_index(op.f('ix_interview_slot_preferences_application_id'), table_name='interview_slot_preferences')
    op.drop_table('interview_slot_preferences')
    op.drop_index(op.f('ix_interview_slots_store_id'), table_name='interview_slots')
    op.drop_table('interview_slots')
