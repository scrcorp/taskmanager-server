"""backfill owner user_stores for existing owners

Revision ID: ad7a5df5aed1
Revises: 7738d6f22819
Create Date: 2026-05-18 22:05:22.822169

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ad7a5df5aed1'
down_revision: Union[str, None] = '7738d6f22819'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 기존 활성 Owner 전원 × 같은 조직의 모든 매장 → user_stores 누락분 INSERT.
    # (is_manager=true, is_work_assignment=false). 알림 수신 + 매장 관리 권한 자동 부여.
    # 이미 user_stores 가 있는 (owner, store) 쌍은 skip.
    # priority 10 = Owner. Super Owner(5)는 매장 운영 비참여 → 제외.
    op.execute("""
        INSERT INTO user_stores (id, user_id, store_id, created_at, is_manager, is_work_assignment)
        SELECT gen_random_uuid(), u.id, s.id, now(), TRUE, FALSE
        FROM users u
        JOIN roles r ON r.id = u.role_id
        JOIN stores s ON s.organization_id = u.organization_id
        WHERE r.priority = 10
          AND u.is_active = TRUE
          AND u.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM user_stores us
              WHERE us.user_id = u.id AND us.store_id = s.id
          )
    """)


def downgrade() -> None:
    # 자동 배정으로 만들어진 (is_manager=true, is_work_assignment=false) Owner 행만 제거.
    # 매뉴얼로 추가된 Owner 매장은 식별 불가하므로 그냥 유지.
    op.execute("""
        DELETE FROM user_stores us
        USING users u, roles r
        WHERE us.user_id = u.id
          AND u.role_id = r.id
          AND r.priority = 10
          AND us.is_manager = TRUE
          AND us.is_work_assignment = FALSE
    """)
