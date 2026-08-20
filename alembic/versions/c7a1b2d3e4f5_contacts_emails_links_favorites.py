"""contacts: 이메일 복수화 + 링크 + 즐겨찾기 + summary/notes (마이그레이션 A = 추가·백필)

구 컬럼(contacts.email / contacts.memo) 제거는 **다음 리비전(B)** 에서 한다.
리네이밍은 create-then-delete 2단계로 한다는 규칙에 따른 것 — A 만 적용된 상태에서도
기존 코드가 그대로 동작해야 롤백이 안전하다.

Revision ID: c7a1b2d3e4f5
Revises: f4a91c7b2e10
Create Date: 2026-08-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7a1b2d3e4f5"
down_revision: Union[str, None] = "f4a91c7b2e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 이메일 복수화 (D7) — 전화번호와 같은 모양 ────────────────────────────
    op.create_table(
        "contact_emails",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=30), nullable=True),
        sa.Column("address", sa.String(length=255), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_contact_emails_contact", "contact_emails", ["contact_id", "sort_order"])
    op.create_index("ix_contact_emails_address", "contact_emails", ["address"])

    # ── 링크 ────────────────────────────────────────────────────────────────
    op.create_table(
        "contact_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=40), nullable=True),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_contact_links_contact", "contact_links", ["contact_id", "sort_order"])

    # ── 즐겨찾기 (개인 설정) ────────────────────────────────────────────────
    op.create_table(
        "contact_favorites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "contact_id", name="uq_contact_favorites"),
    )
    op.create_index("ix_contact_favorites_user", "contact_favorites", ["user_id"])

    # ── summary / notes (D8) ────────────────────────────────────────────────
    # notes 는 Text 다. 상한 300 은 **새 입력에만** 스키마에서 건다 —
    # 기존 memo 는 4000 까지 허용됐고, 길다는 이유로 열람·수정이 막히면 안 된다.
    op.add_column("contacts", sa.Column("summary", sa.String(length=72), nullable=True))
    op.add_column("contacts", sa.Column("notes", sa.Text(), nullable=True))

    # ── 백필 ────────────────────────────────────────────────────────────────
    # 기존 단일 email → 자식 테이블 1행(대표). 공백만 있는 값은 옮기지 않는다.
    op.execute(
        """
        INSERT INTO contact_emails (id, contact_id, label, address, is_primary, sort_order, created_at)
        SELECT gen_random_uuid(), id, NULL, btrim(email), true, 0, now()
        FROM contacts
        WHERE email IS NOT NULL AND btrim(email) <> ''
        """
    )
    # memo → notes (내용 그대로. 길이 제한은 걸지 않는다)
    op.execute("UPDATE contacts SET notes = memo WHERE memo IS NOT NULL")


def downgrade() -> None:
    op.drop_column("contacts", "notes")
    op.drop_column("contacts", "summary")
    op.drop_index("ix_contact_favorites_user", table_name="contact_favorites")
    op.drop_table("contact_favorites")
    op.drop_index("ix_contact_links_contact", table_name="contact_links")
    op.drop_table("contact_links")
    op.drop_index("ix_contact_emails_address", table_name="contact_emails")
    op.drop_index("ix_contact_emails_contact", table_name="contact_emails")
    op.drop_table("contact_emails")
