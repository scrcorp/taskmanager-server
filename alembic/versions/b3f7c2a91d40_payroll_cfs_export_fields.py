"""payroll CFS export fields — group payroll ids, tip eligibility, bonus rate

회계사(CFS) 급여 파일 양식 대응. 결정 근거는
docs/99_inbox/2026-08-11-payroll-cfs-export-결정사항.md.

추가 내용:
    - store_groups.payroll_corp_name  (corp 표시명 — 시트 식별자는 기존 code 필드가 겸함.
      구 payroll_code 는 dev 의 StoreGroup.code 와 중복이라 머지 시 code 로 통합, 컬럼 미생성)
    - org_member_stores.tip_eligible / bonus_rate     (직원 × 매장 단위 값)
    - payroll_entries.bonus_pay                       (보너스 지급액, gross 에 포함)
    - bonus_rate_history                              (보너스율 변경 이력, 시급과 동일 규칙)
    - settings_registry: payroll.tip_distribution_mode / payroll.performance_bonus_enabled

settings 는 시드가 INSERT-only 라 신규 키는 이 마이그레이션이 INSERT 주체다
(app/seeds/settings_seed.py 상단 주석 참조 — 양쪽 정의를 항상 같이 고칠 것).

Revision ID: b3f7c2a91d40
Revises: e9c3d4f5a6b7
Create Date: 2026-08-11

(2026-08-19 group-scope 트랙 머지 시 dev head e9c3d4f5a6b7 뒤로 직렬 재연결 —
 관례상 merge migration 대신 down_revision 재지정. 원래 Revises: a7c1e9d40b52)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3f7c2a91d40"
down_revision: Union[str, None] = "e9c3d4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# settings_registry 신규 키 — (key, label, description, value_type, default_value_json, category)
_SETTINGS = [
    (
        "payroll.tip_distribution_mode",
        "Tip distribution",
        "How tips collected at this store are split. 'none' = this store does not run tips "
        "(staff tip eligibility is forced off). 'manual' = whoever received the tips assigns "
        "amounts to co-workers. 'hours_prorated' = split automatically every day across "
        "eligible staff by hours worked, counting at most 8 hours per person.",
        "string",
        '"none"',
        "Payroll",
    ),
    (
        "payroll.performance_bonus_enabled",
        "Performance bonus",
        "Allow an hourly add-on rate per staff member at this store. When disabled, bonus "
        "rates cannot be set and no bonus is paid.",
        "boolean",
        "false",
        "Payroll",
    ),
]


def upgrade() -> None:
    op.add_column(
        "store_groups", sa.Column("payroll_corp_name", sa.String(length=255), nullable=True)
    )

    op.add_column(
        "org_member_stores",
        sa.Column(
            "tip_eligible", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )
    op.add_column(
        "org_member_stores", sa.Column("bonus_rate", sa.Numeric(10, 2), nullable=True)
    )

    op.add_column(
        "payroll_entries",
        sa.Column(
            "bonus_pay", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0")
        ),
    )

    op.create_table(
        "bonus_rate_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("org_member_store_id", sa.Uuid(), nullable=False),
        sa.Column("old_rate", sa.Numeric(10, 2), nullable=True),
        sa.Column("new_rate", sa.Numeric(10, 2), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        # 급여 근거 데이터 — 배정 삭제로 소멸 금지
        sa.ForeignKeyConstraint(
            ["org_member_store_id"], ["org_member_stores.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_member_store_id",
            "effective_date",
            name="uq_bonus_history_member_store_effective",
        ),
        sa.CheckConstraint("new_rate >= 0", name="ck_bonus_history_new_rate_non_negative"),
    )
    op.create_index(
        "ix_bonus_rate_history_organization_id", "bonus_rate_history", ["organization_id"]
    )
    op.create_index(
        "ix_bonus_history_member_store_effective",
        "bonus_rate_history",
        ["org_member_store_id", sa.text("effective_date DESC")],
    )

    for key, label, desc, vtype, default_json, category in _SETTINGS:
        op.execute(
            sa.text(
                """
                INSERT INTO settings_registry
                    (key, label, description, value_type, default_value,
                     category, levels, default_priority, created_at, updated_at)
                VALUES
                    (:key, :label, :description, :value_type,
                     CAST(:default_value AS jsonb), :category,
                     CAST('["org", "store"]' AS jsonb), 'item', now(), now())
                ON CONFLICT (key) DO NOTHING
                """
            ).bindparams(
                key=key,
                label=label,
                description=desc,
                value_type=vtype,
                default_value=default_json,
                category=category,
            )
        )


def downgrade() -> None:
    for key, *_ in _SETTINGS:
        op.execute(
            sa.text("DELETE FROM settings_registry WHERE key = :key").bindparams(key=key)
        )
    op.drop_index("ix_bonus_history_member_store_effective", table_name="bonus_rate_history")
    op.drop_index("ix_bonus_rate_history_organization_id", table_name="bonus_rate_history")
    op.drop_table("bonus_rate_history")
    op.drop_column("payroll_entries", "bonus_pay")
    op.drop_column("org_member_stores", "bonus_rate")
    op.drop_column("org_member_stores", "tip_eligible")
    op.drop_column("store_groups", "payroll_corp_name")
