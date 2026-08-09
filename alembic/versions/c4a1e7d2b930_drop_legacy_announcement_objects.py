"""모델↔DB 드리프트 정리 (1/2) — 손실 없는 잔재 제거

배경: 이 repo 는 모델에서 지운 테이블/컬럼을 DB 에서 안 지운 잔재가 쌓여 있었다.
그 상태로 `alembic revision --autogenerate` 를 돌리면 알렘빅이 "모델에 없다" =
"지워야 한다" 로 해석해서 drop_table/drop_index 를 무더기로 뱉는다. 배포는
`alembic upgrade head` 가 자동으로 도니, 그 결과를 검토 없이 커밋하는 순간
운영 테이블과 인덱스가 사라진다.

이 마이그레이션은 그 중 **데이터 손실이 0 인 것만** 정리한다:

  - announcements       (0행) — 구 공지 시스템. 현재는 notices 가 대체.
  - announcement_reads  (0행) — 위 테이블의 읽음 추적.
  - file_usages.sort_order NOT NULL — 모델은 이미 NOT NULL 인데 DB 만 nullable
    이었다. NULL 행 0/5517 이라 그대로 승격 가능.

인덱스 9개(alerts/notices/reports/store_hiring_forms/task_comments/tasks)는
DDL 변경이 아니라 **모델에 선언을 추가**하는 것으로 해소했다 — 그 인덱스들은
잔재가 아니라 실제로 쓰이는 정상 인덱스였고, 그 중 2개는 중복 데이터를 막는
partial UNIQUE 였다.

남은 2건은 지우면 안 되는 것이라 alembic 쪽에서 제외 처리했다 (env.py 참조):
  - notifications (1479행) — 실데이터 보존.
  - users.notification_preferences — **잔재가 아니라 의도적인 하위호환 컬럼**.
    42abeece1bb2 가 구버전 API 를 위해 되살렸고 alert_preferences 와 트리거
    (tr_sync_user_pref_columns) 로 양방향 동기화 중이다. 실제로 이 컬럼을
    drop 해봤더니 트리거가 깨져 users UPDATE 가 전부 실패했다
    (record "new" has no field "notification_preferences") — autogenerate 가
    보지 못하는 의존성의 실례다.

Revision ID: c4a1e7d2b930
Revises: b075fd4d7262
Create Date: 2026-08-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4a1e7d2b930'
down_revision: Union[str, None] = 'b075fd4d7262'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 자식 테이블 먼저 (FK: announcement_reads → announcements)
    op.drop_table('announcement_reads')
    op.drop_table('announcements')

    # 모델이 이미 NOT NULL — DB 만 뒤처져 있었다. NULL 행이 없으므로 무손실.
    op.alter_column(
        'file_usages',
        'sort_order',
        existing_type=sa.Integer(),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        'file_usages',
        'sort_order',
        existing_type=sa.Integer(),
        nullable=True,
    )

    # 구조만 복원한다 — 행이 0개였으므로 데이터 복원은 필요 없다.
    op.create_table(
        'announcements',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('store_id', sa.Uuid(), nullable=True),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(
            ['organization_id'], ['organizations.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
    )
    op.create_table(
        'announcement_reads',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('announcement_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column(
            'read_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(
            ['announcement_id'], ['announcements.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.UniqueConstraint(
            'announcement_id', 'user_id', name='uq_announcement_read'
        ),
    )
