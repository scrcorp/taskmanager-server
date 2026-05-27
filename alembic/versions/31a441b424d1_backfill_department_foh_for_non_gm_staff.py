"""backfill department FOH for non-GM staff

Revision ID: 31a441b424d1
Revises: 6bc9c7a72350
Create Date: 2026-05-27 15:32:19.050153

초기 일괄 지정 (사용자 승인 2026-05-27):
GM 미만(= role priority > GM_PRIORITY, 즉 SV·staff) 직원의 department 를 'FOH' 로 채운다.
GM 이상(GM/Owner/SuperOwner, priority <= GM_PRIORITY)은 미지정(NULL) 유지.
이미 값이 있는 직원은 건드리지 않음 (department IS NULL 조건).

배포 시 staging/prod 기존 데이터에도 적용된다.
"""
from typing import Sequence, Union

from alembic import op

from app.core.permissions import GM_PRIORITY

# revision identifiers, used by Alembic.
revision: str = '31a441b424d1'
down_revision: Union[str, None] = '6bc9c7a72350'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # GM 미만(SV·staff)만 FOH. 이미 지정된 값은 보존.
    op.execute(
        f"""
        UPDATE users
        SET department = 'FOH'
        WHERE department IS NULL
          AND role_id IN (
              SELECT id FROM roles WHERE priority > {GM_PRIORITY}
          )
        """
    )


def downgrade() -> None:
    # best-effort 되돌리기 — 백필 규칙에 해당하는(GM 미만) FOH 를 NULL 로.
    # 수동 지정한 FOH 와 구분 불가하므로 데이터 손실 가능 (data migration 특성).
    op.execute(
        f"""
        UPDATE users
        SET department = NULL
        WHERE department = 'FOH'
          AND role_id IN (
              SELECT id FROM roles WHERE priority > {GM_PRIORITY}
          )
        """
    )
