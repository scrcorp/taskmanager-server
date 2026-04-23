"""add attendance_devices + access_codes + users.clockin_pin

Revision ID: 2ab49c216eb8
Revises: 46f7fd01335b
Create Date: 2026-04-22 17:38:18.193967

"""
import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2ab49c216eb8'
down_revision: Union[str, None] = '46f7fd01335b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _generate_pin() -> str:
    """랜덤 6자리 숫자 PIN 생성 (선행 0 허용)."""
    return f"{secrets.randbelow(1_000_000):06d}"


def upgrade() -> None:
    # ── 스키마 변경 (autogenerate) ─────────────────────────────
    op.create_table(
        'access_codes',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('service_key', sa.String(length=50), nullable=False),
        sa.Column('code', sa.String(length=32), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('service_key'),
    )
    op.create_table(
        'attendance_devices',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('store_id', sa.Uuid(), nullable=True),
        sa.Column('device_name', sa.String(length=100), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('fingerprint', sa.String(length=255), nullable=True),
        sa.Column('registered_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index('ix_attendance_devices_org_active', 'attendance_devices', ['organization_id', 'revoked_at'], unique=False)
    op.create_index('ix_attendance_devices_store', 'attendance_devices', ['store_id'], unique=False)
    op.add_column('users', sa.Column('clockin_pin', sa.String(length=6), nullable=True))
    op.create_unique_constraint('uq_user_org_clockin_pin', 'users', ['organization_id', 'clockin_pin'])

    # ── 데이터 백필: 기존 유저에게 organization 단위 unique 랜덤 PIN 발급 ──
    # 한 조직 내에서 이미 사용 중인 PIN은 피하고, 재시도 안에 못 찾으면 로그 후 null 유지.
    conn = op.get_bind()
    users_by_org: dict = {}
    rows = conn.execute(sa.text("SELECT id, organization_id FROM users WHERE clockin_pin IS NULL")).fetchall()
    for user_id, org_id in rows:
        users_by_org.setdefault(org_id, []).append(user_id)

    for org_id, user_ids in users_by_org.items():
        used: set[str] = set()
        for user_id in user_ids:
            pin = None
            for _ in range(50):
                candidate = _generate_pin()
                if candidate in used:
                    continue
                # DB에도 없는지 확인 (배포 환경에서 부분 백필된 상태 대비)
                exists = conn.execute(
                    sa.text("SELECT 1 FROM users WHERE organization_id = :o AND clockin_pin = :p"),
                    {"o": org_id, "p": candidate},
                ).first()
                if exists:
                    continue
                pin = candidate
                used.add(pin)
                break
            if pin is None:
                # 6자리 공간(100만 개)보다 유저가 많거나 경쟁이 극심한 경우. 조직당 수만 명 규모면 확장 필요.
                continue
            conn.execute(
                sa.text("UPDATE users SET clockin_pin = :p WHERE id = :id"),
                {"p": pin, "id": user_id},
            )


def downgrade() -> None:
    op.drop_constraint('uq_user_org_clockin_pin', 'users', type_='unique')
    op.drop_column('users', 'clockin_pin')
    op.drop_index('ix_attendance_devices_store', table_name='attendance_devices')
    op.drop_index('ix_attendance_devices_org_active', table_name='attendance_devices')
    op.drop_table('attendance_devices')
    op.drop_table('access_codes')
