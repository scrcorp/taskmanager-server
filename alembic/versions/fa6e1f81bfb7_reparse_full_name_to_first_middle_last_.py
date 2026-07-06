"""reparse full_name to first/middle/last (last=last word, middle=rest)

Revision ID: fa6e1f81bfb7
Revises: 8d6787f211c0
Create Date: 2026-07-03 17:49:58.734624

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fa6e1f81bfb7'
down_revision: Union[str, None] = '8d6787f211c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 이전 S7-2 백필이 "첫 토큰=first, 나머지 전부=last"로 잘못 파싱했던 것을 재교정.
    # 규칙: first=맨 앞 단어, last=맨 뒤 단어, middle=그 사이 전부. (override — 전 행 재파싱)
    op.execute(
        """
        UPDATE users u
        SET first_name = w.arr[1],
            last_name = CASE WHEN w.n >= 2 THEN w.arr[w.n] ELSE NULL END,
            middle_name = CASE WHEN w.n >= 3
                               THEN array_to_string(w.arr[2:w.n - 1], ' ')
                               ELSE NULL END
        FROM (
            SELECT id,
                   regexp_split_to_array(btrim(full_name), '\\s+') AS arr,
                   array_length(regexp_split_to_array(btrim(full_name), '\\s+'), 1) AS n
            FROM users
            WHERE full_name IS NOT NULL AND btrim(full_name) <> ''
        ) w
        WHERE u.id = w.id
        """
    )


def downgrade() -> None:
    pass
