"""add staff_work_patterns + schedules pattern stamp (Fixed Schedule)

고정 근무(반복 패턴) 원장 신설 + schedules 도장 3컬럼
(SoT: docs/99_inbox/2026-08-20-고정근무-구현계약.md §1):
    - staff_work_patterns: 블록 1개 = 행 1개, group_id 로 묶음. 시간은 store tz 벽시계,
      byday 는 0=Sun..6=Sat(rrule BYDAY 투영). (user, store, dow) 유일성은 두지 않는다.
    - schedules.pattern_id(SET NULL) / pattern_occurrence_date / pattern_overridden
    - uq_schedules_pattern_occurrence: (pattern_id, pattern_occurrence_date) WHERE pattern_id
      IS NOT NULL — **status 조건 없음** (deleted 행도 슬롯 점유).
    - ck_schedules_no_virtual: 'virtual' 은 응답 전용, 저장 금지.
    - ck_schedules_pattern_pair: pattern_id 와 pattern_occurrence_date 는 항상 쌍.

Revision ID: b7c8d9e0f1a2
Revises: aa789ecf877b
Create Date: 2026-08-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, None] = "aa789ecf877b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. 패턴 원장 ──────────────────────────────────────────
    op.create_table(
        "staff_work_patterns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("store_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # 한 설정창에서 저장한 블록 묶음 (FK 아님)
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("work_role_id", sa.Uuid(), nullable=True),
        # RFC 5545 원문 — v1 은 FREQ=WEEKLY 만
        sa.Column("rrule", sa.Text(), nullable=False),
        # BYDAY 투영, 0=Sun..6=Sat
        sa.Column("byday", postgresql.ARRAY(sa.SmallInteger()), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("break_start_time", sa.Time(), nullable=True),
        sa.Column("break_end_time", sa.Time(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        # NULL = 무기한
        sa.Column("until_date", sa.Date(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_role_id"], ["store_work_roles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        # overnight 허용 — 같은 시각만 거부
        sa.CheckConstraint("end_time <> start_time", name="ck_staff_work_patterns_times"),
        sa.CheckConstraint(
            "until_date IS NULL OR until_date >= start_date",
            name="ck_staff_work_patterns_period",
        ),
        sa.CheckConstraint("cardinality(byday) > 0", name="ck_staff_work_patterns_byday"),
    )
    op.create_index(
        "ix_staff_work_patterns_organization_id", "staff_work_patterns", ["organization_id"]
    )
    op.create_index("ix_staff_work_patterns_user_id", "staff_work_patterns", ["user_id"])
    op.create_index("ix_staff_work_patterns_group_id", "staff_work_patterns", ["group_id"])
    op.create_index(
        "ix_staff_work_patterns_org_store_user",
        "staff_work_patterns",
        ["organization_id", "store_id", "user_id"],
    )

    # ── 2. schedules 도장 ─────────────────────────────────────
    op.add_column("schedules", sa.Column("pattern_id", sa.Uuid(), nullable=True))
    op.add_column("schedules", sa.Column("pattern_occurrence_date", sa.Date(), nullable=True))
    op.add_column(
        "schedules",
        sa.Column(
            "pattern_overridden",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_foreign_key(
        "fk_schedules_pattern_id_staff_work_patterns",
        "schedules",
        "staff_work_patterns",
        ["pattern_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_schedules_pattern_id", "schedules", ["pattern_id"])
    # 슬롯 유일성 — status 조건 없음 (deleted 도 점유)
    op.create_index(
        "uq_schedules_pattern_occurrence",
        "schedules",
        ["pattern_id", "pattern_occurrence_date"],
        unique=True,
        postgresql_where=sa.text("pattern_id IS NOT NULL"),
    )
    op.create_check_constraint("ck_schedules_no_virtual", "schedules", "status <> 'virtual'")
    op.create_check_constraint(
        "ck_schedules_pattern_pair",
        "schedules",
        "(pattern_id IS NULL) = (pattern_occurrence_date IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_schedules_pattern_pair", "schedules", type_="check")
    op.drop_constraint("ck_schedules_no_virtual", "schedules", type_="check")
    op.drop_index("uq_schedules_pattern_occurrence", table_name="schedules")
    op.drop_index("ix_schedules_pattern_id", table_name="schedules")
    op.drop_constraint("fk_schedules_pattern_id_staff_work_patterns", "schedules", type_="foreignkey")
    op.drop_column("schedules", "pattern_overridden")
    op.drop_column("schedules", "pattern_occurrence_date")
    op.drop_column("schedules", "pattern_id")

    op.drop_index("ix_staff_work_patterns_org_store_user", table_name="staff_work_patterns")
    op.drop_index("ix_staff_work_patterns_group_id", table_name="staff_work_patterns")
    op.drop_index("ix_staff_work_patterns_user_id", table_name="staff_work_patterns")
    op.drop_index("ix_staff_work_patterns_organization_id", table_name="staff_work_patterns")
    op.drop_table("staff_work_patterns")
