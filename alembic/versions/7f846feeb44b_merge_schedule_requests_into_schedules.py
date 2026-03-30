"""merge schedule_requests into schedules

Revision ID: 7f846feeb44b
Revises: b1c2e3f4g5h7
Create Date: 2026-03-30 18:53:12.586841

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '7f846feeb44b'
down_revision: Union[str, None] = 'b1c2e3f4g5h7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add new columns to schedules
    op.add_column('schedules', sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('schedules', sa.Column('is_modified', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('schedules', sa.Column('rejection_reason', sa.Text(), nullable=True))
    op.add_column('schedules', sa.Column('modifications', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_index('ix_schedules_status', 'schedules', ['status'], unique=False)

    # 2. Migrate schedule_requests data into schedules (only non-rejected requests without existing schedule)
    op.execute("""
        INSERT INTO schedules (
            id, organization_id, request_id, user_id, store_id, work_role_id,
            work_date, start_time, end_time, break_start_time, break_end_time,
            net_work_minutes, status, created_by, note, hourly_rate,
            submitted_at, is_modified, rejection_reason, created_at, updated_at
        )
        SELECT
            sr.id,
            s.organization_id,
            sr.id,
            sr.user_id,
            sr.store_id,
            sr.work_role_id,
            sr.work_date,
            sr.preferred_start_time,
            sr.preferred_end_time,
            sr.break_start_time,
            sr.break_end_time,
            CASE
                WHEN sr.preferred_start_time IS NOT NULL AND sr.preferred_end_time IS NOT NULL THEN
                    GREATEST(0,
                        (EXTRACT(HOUR FROM sr.preferred_end_time) * 60 + EXTRACT(MINUTE FROM sr.preferred_end_time))
                        - (EXTRACT(HOUR FROM sr.preferred_start_time) * 60 + EXTRACT(MINUTE FROM sr.preferred_start_time))
                        + CASE WHEN sr.preferred_end_time <= sr.preferred_start_time THEN 1440 ELSE 0 END
                        - CASE
                            WHEN sr.break_start_time IS NOT NULL AND sr.break_end_time IS NOT NULL THEN
                                (EXTRACT(HOUR FROM sr.break_end_time) * 60 + EXTRACT(MINUTE FROM sr.break_end_time))
                                - (EXTRACT(HOUR FROM sr.break_start_time) * 60 + EXTRACT(MINUTE FROM sr.break_start_time))
                                + CASE WHEN sr.break_end_time <= sr.break_start_time THEN 1440 ELSE 0 END
                            ELSE 0
                        END
                    )
                ELSE 0
            END,
            CASE sr.status
                WHEN 'submitted' THEN 'requested'
                WHEN 'accepted' THEN 'requested'
                WHEN 'modified' THEN 'requested'
                WHEN 'rejected' THEN 'rejected'
                ELSE 'requested'
            END,
            sr.created_by,
            sr.note,
            sr.hourly_rate,
            sr.submitted_at,
            CASE WHEN sr.original_preferred_start_time IS NOT NULL THEN true ELSE false END,
            sr.rejection_reason,
            sr.created_at,
            sr.updated_at
        FROM schedule_requests sr
        JOIN stores s ON s.id = sr.store_id
        WHERE NOT EXISTS (
            SELECT 1 FROM schedules sc WHERE sc.request_id = sr.id
        )
    """)


def downgrade() -> None:
    # Remove migrated request data (those with submitted_at and status='requested')
    op.execute("DELETE FROM schedules WHERE submitted_at IS NOT NULL AND request_id IS NOT NULL AND status IN ('requested', 'rejected')")
    op.drop_index('ix_schedules_status', table_name='schedules')
    op.drop_column('schedules', 'modifications')
    op.drop_column('schedules', 'rejection_reason')
    op.drop_column('schedules', 'is_modified')
    op.drop_column('schedules', 'submitted_at')
