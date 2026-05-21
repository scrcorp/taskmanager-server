"""users.clockin_pin: reassign + add unique per org

Revision ID: 10cd6f743384
Revises: 675687154768
Create Date: 2026-05-21 13:49:31.583824

조직 내 clockin_pin unique 제약 추가.
기존 clockin_pin 값은 모두 새 랜덤 6자리로 재배정 (옵션 C).

⚠️ 비가역적 데이터 변경:
- upgrade() 가 모든 직원의 기존 PIN을 새 랜덤 값으로 덮어씀
- downgrade() 는 unique 제약만 제거. PIN 원본 값은 복원 불가
- prod 배포 전 매니저/직원 통보 필수 (모든 PIN이 바뀜)
"""
from typing import Sequence, Union
import secrets
from collections import defaultdict

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '10cd6f743384'
down_revision: Union[str, None] = '675687154768'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _gen_pin() -> str:
    """6자리 숫자 PIN. app.services.attendance_device_service.generate_clockin_pin 과 동일 로직."""
    return f"{secrets.randbelow(1_000_000):06d}"


def upgrade() -> None:
    conn = op.get_bind()

    # 1. clockin_pin 이 있는 모든 user 를 org 별로 그룹화
    rows = conn.execute(
        sa.text("SELECT id, organization_id FROM users WHERE clockin_pin IS NOT NULL")
    ).fetchall()
    users_by_org: dict = defaultdict(list)
    for row in rows:
        users_by_org[row.organization_id].append(row.id)

    # 2. 각 org 별로 인원수만큼 unique 한 PIN 을 set 으로 채워 일괄 UPDATE
    #    set 자동 dedup 으로 충돌 회피 — retry/사전 SELECT 불필요.
    for org_id, user_ids in users_by_org.items():
        pins: set[str] = set()
        while len(pins) < len(user_ids):
            pins.add(_gen_pin())
        for user_id, pin in zip(user_ids, pins):
            conn.execute(
                sa.text("UPDATE users SET clockin_pin = :pin WHERE id = :id"),
                {"pin": pin, "id": user_id},
            )

    # 3. unique 제약 추가 — (organization_id, clockin_pin)
    #    NULL clockin_pin 은 PostgreSQL 기본 동작상 여러 row 허용됨.
    op.create_unique_constraint(
        'uq_user_org_clockin_pin', 'users', ['organization_id', 'clockin_pin']
    )


def downgrade() -> None:
    # PIN 원본 값은 복원 불가. 제약만 제거.
    op.drop_constraint('uq_user_org_clockin_pin', 'users', type_='unique')
