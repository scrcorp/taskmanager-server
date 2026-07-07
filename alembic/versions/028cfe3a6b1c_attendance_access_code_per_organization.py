"""attendance access_code per organization

access_codes 를 (service_key, organization) 별 코드로 전환한다.
- organization_id 컬럼 추가
- 기존 전역 unique(service_key) 제거 → 조직별 unique(service_key, organization_id)
  + 코드 역조회 unique(service_key, code) 추가
- 데이터: 기존 전역 attendance 코드는 가장 오래된 org 에 귀속시키고(그 org 는 코드 유지),
  나머지 활성 org 에는 유니크 코드를 새로 발급한다.

Revision ID: 028cfe3a6b1c
Revises: 832ddded650f
Create Date: 2026-07-06 14:47:40.707265

"""
import secrets
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = '028cfe3a6b1c'
down_revision: Union[str, None] = '832ddded650f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def upgrade() -> None:
    bind = op.get_bind()

    # 1) organization_id 컬럼 (nullable)
    op.add_column('access_codes', sa.Column('organization_id', sa.Uuid(), nullable=True))

    # 2) 기존 전역 attendance 코드 → 가장 오래된 org 에 귀속 (그 org 는 코드 그대로 유지)
    oldest = bind.execute(
        text("SELECT id FROM organizations ORDER BY created_at ASC LIMIT 1")
    ).scalar()
    if oldest is not None:
        bind.execute(
            text(
                "UPDATE access_codes SET organization_id = :oid "
                "WHERE service_key = 'attendance' AND organization_id IS NULL"
            ),
            {"oid": oldest},
        )

    # 3) 전역 unique 제거 → FK + 조직별 unique + 코드 역조회 unique
    op.drop_constraint('access_codes_service_key_key', 'access_codes', type_='unique')
    op.create_foreign_key(
        'fk_access_codes_org', 'access_codes', 'organizations',
        ['organization_id'], ['id'], ondelete='CASCADE',
    )
    op.create_unique_constraint(
        'uq_access_code_service_code', 'access_codes', ['service_key', 'code']
    )
    op.create_unique_constraint(
        'uq_access_code_service_org', 'access_codes', ['service_key', 'organization_id']
    )

    # 4) 나머지 활성 org 에 attendance 코드 발급 (service_key 내 전역 유니크 보장)
    existing_codes = {
        r[0] for r in bind.execute(
            text("SELECT code FROM access_codes WHERE service_key = 'attendance'")
        ).all()
    }

    def _gen() -> str:
        while True:
            candidate = "".join(secrets.choice(_ALPHABET) for _ in range(6))
            if candidate not in existing_codes:
                existing_codes.add(candidate)
                return candidate

    orgs_without = bind.execute(
        text(
            "SELECT o.id FROM organizations o "
            "WHERE o.is_active = true AND NOT EXISTS ("
            "  SELECT 1 FROM access_codes a "
            "  WHERE a.service_key = 'attendance' AND a.organization_id = o.id)"
        )
    ).all()
    for (oid,) in orgs_without:
        bind.execute(
            text(
                "INSERT INTO access_codes (id, service_key, organization_id, code, source, created_at) "
                "VALUES (:id, 'attendance', :oid, :code, 'auto', now())"
            ),
            {"id": str(uuid.uuid4()), "oid": oid, "code": _gen()},
        )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_constraint('uq_access_code_service_org', 'access_codes', type_='unique')
    op.drop_constraint('uq_access_code_service_code', 'access_codes', type_='unique')
    op.drop_constraint('fk_access_codes_org', 'access_codes', type_='foreignkey')

    # 전역 unique(service_key) 복원을 위해, 서비스별로 가장 오래된 org 코드만 남기고 나머지 삭제.
    bind.execute(
        text(
            "DELETE FROM access_codes a USING organizations o "
            "WHERE a.service_key = 'attendance' AND a.organization_id = o.id "
            "AND o.id <> (SELECT id FROM organizations ORDER BY created_at ASC LIMIT 1)"
        )
    )
    op.create_unique_constraint('access_codes_service_key_key', 'access_codes', ['service_key'])
    op.drop_column('access_codes', 'organization_id')
