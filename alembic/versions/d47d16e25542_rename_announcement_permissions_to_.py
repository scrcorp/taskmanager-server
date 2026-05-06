"""rename announcement permissions to notice

Revision ID: d47d16e25542
Revises: 5404355df377
Create Date: 2026-05-06 11:10:06.686069

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd47d16e25542'
down_revision: Union[str, None] = '5404355df377'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """기존 announcements:* permission 정리.

    PERMISSION_REGISTRY 자동 sync 로 notices:* 가 이미 추가됨. 같은 권한을
    가진 role 들에 양쪽이 다 매핑되어 있으므로 옛 announcements:* 행만 정리.
    role_permissions FK 도 cascade 또는 명시 삭제.
    """
    # 1. role_permissions 에서 옛 announcements:* 매핑 제거
    op.execute("""
        DELETE FROM role_permissions
        WHERE permission_id IN (
            SELECT id FROM permissions WHERE code LIKE 'announcements:%'
        )
    """)
    # 2. permissions 테이블에서 옛 코드 제거
    op.execute("DELETE FROM permissions WHERE code LIKE 'announcements:%'")


def downgrade() -> None:
    # 옛 코드 복구는 PERMISSION_REGISTRY 의 sync 가 처리하지 않음.
    # 수동 복구 필요 시 별도 마이그레이션. 일단 noop.
    pass
