"""schedule audit log table snapshot and cancel reject metadata

Revision ID: 6cd6eabf2d1d
Revises: 725d4e909a60
Create Date: 2026-04-07 14:21:34.668122

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '6cd6eabf2d1d'
down_revision: Union[str, None] = '725d4e909a60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('schedule_audit_logs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('schedule_id', sa.Uuid(), nullable=False),
        sa.Column('event_type', sa.String(length=20), nullable=False),
        sa.Column('actor_id', sa.Uuid(), nullable=True),
        sa.Column('actor_role', sa.String(length=20), nullable=True),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('diff', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='SET NULL', name='fk_schedule_audit_logs_actor_id'),
        sa.ForeignKeyConstraint(['schedule_id'], ['schedules.id'], ondelete='CASCADE', name='fk_schedule_audit_logs_schedule_id'),
        sa.PrimaryKeyConstraint('id', name='schedule_audit_logs_pkey'),
    )
    op.create_index('ix_schedule_audit_logs_schedule_ts', 'schedule_audit_logs', ['schedule_id', 'timestamp'], unique=False)

    op.add_column('schedules', sa.Column('work_role_name_snapshot', sa.String(length=100), nullable=True))
    op.add_column('schedules', sa.Column('position_snapshot', sa.String(length=100), nullable=True))
    op.add_column('schedules', sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('schedules', sa.Column('rejected_by', sa.Uuid(), nullable=True))
    op.add_column('schedules', sa.Column('rejected_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('schedules', sa.Column('cancelled_by', sa.Uuid(), nullable=True))
    op.add_column('schedules', sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('schedules', sa.Column('cancellation_reason', sa.Text(), nullable=True))
    op.create_foreign_key('fk_schedules_cancelled_by', 'schedules', 'users', ['cancelled_by'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('fk_schedules_rejected_by', 'schedules', 'users', ['rejected_by'], ['id'], ondelete='SET NULL')

    # ─── Data migration: 기존 schedules.modifications JSONB → schedule_audit_logs ───
    # modifications: [{field, old_value, new_value, modified_by, modified_at}, ...]
    # 각 항목을 audit_log row로 변환 (event_type='modified')
    op.execute("""
        INSERT INTO schedule_audit_logs (id, schedule_id, event_type, actor_id, timestamp, description, diff)
        SELECT
            gen_random_uuid(),
            s.id,
            'modified',
            CASE
                WHEN m->>'modified_by' ~ '^[0-9a-fA-F-]{36}$' THEN (m->>'modified_by')::uuid
                ELSE NULL
            END,
            COALESCE(
                CASE
                    WHEN m->>'modified_at' IS NOT NULL THEN (m->>'modified_at')::timestamptz
                    ELSE NULL
                END,
                s.updated_at
            ),
            'Legacy modification (migrated from schedules.modifications)',
            jsonb_build_object(
                m->>'field',
                jsonb_build_object('old', m->'old_value', 'new', m->'new_value')
            )
        FROM schedules s,
             LATERAL jsonb_array_elements(s.modifications) AS m
        WHERE s.modifications IS NOT NULL
          AND jsonb_typeof(s.modifications) = 'array'
          AND jsonb_array_length(s.modifications) > 0
    """)

    # ─── Backfill confirmed_at for existing confirmed schedules ───
    op.execute("UPDATE schedules SET confirmed_at = updated_at WHERE status = 'confirmed' AND confirmed_at IS NULL")


def downgrade() -> None:
    op.drop_constraint('fk_schedules_rejected_by', 'schedules', type_='foreignkey')
    op.drop_constraint('fk_schedules_cancelled_by', 'schedules', type_='foreignkey')
    op.drop_column('schedules', 'cancellation_reason')
    op.drop_column('schedules', 'cancelled_at')
    op.drop_column('schedules', 'cancelled_by')
    op.drop_column('schedules', 'rejected_at')
    op.drop_column('schedules', 'rejected_by')
    op.drop_column('schedules', 'confirmed_at')
    op.drop_column('schedules', 'position_snapshot')
    op.drop_column('schedules', 'work_role_name_snapshot')
    op.drop_index('ix_schedule_audit_logs_schedule_ts', table_name='schedule_audit_logs')
    op.drop_table('schedule_audit_logs')
