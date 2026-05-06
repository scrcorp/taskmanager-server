"""backfill reference_type for legacy alerts

Revision ID: ee2d6feb662e
Revises: 42abeece1bb2
Create Date: 2026-05-06 11:41:41.479621

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ee2d6feb662e'
down_revision: Union[str, None] = '42abeece1bb2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """옛 알림 데이터의 reference_type NULL 을 type 기반으로 backfill.

    admin/app 의 routing 함수가 reference_type 으로 분기하는데 NULL 이면
    클릭 시 dead-link. type 으로 reference_type 을 안전하게 추론 가능한
    경우만 채움. (reply 는 checklist_review/daily_report 모호로 제외.)

    notifications 테이블도 동일하게 backfill (단방향 sync trigger 라
    notifications 갱신은 alerts 로 자동 sync 되지 않음).
    """
    type_to_ref = [
        ("additional_task", "additional_task"),
        ("schedule_pending", "schedule"),
        ("schedule_approved", "schedule"),
        ("schedule_substitute", "schedule"),
        ("work_assigned", "schedule"),  # legacy
        ("checklist_submitted", "cl_instances"),
        ("checklist_report", "cl_instances"),  # legacy
        ("checklist_re_review", "cl_instance_items"),
        ("attendance_corrected", "attendance"),
        ("notice", "notice"),
        ("announcement", "notice"),  # legacy
    ]
    for table in ("alerts", "notifications"):
        for alert_type, ref_type in type_to_ref:
            op.execute(f"""
                UPDATE {table}
                SET reference_type = '{ref_type}'
                WHERE type = '{alert_type}' AND reference_type IS NULL
            """)


def downgrade() -> None:
    # 데이터 backfill 만 하므로 downgrade 불필요 (NULL 로 되돌리면 또 dead-link).
    pass
