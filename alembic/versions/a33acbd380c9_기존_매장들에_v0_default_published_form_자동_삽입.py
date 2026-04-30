"""기존 매장들에 v0 default published form 자동 삽입

published form 이 하나도 없는 매장에 v0 (DEFAULT_FORM_CONFIG) row 를 삽입.
이후 매장 생성 로직에서도 자동 삽입되므로 신규 매장은 처음부터 v0 가짐.

Revision ID: a33acbd380c9
Revises: 517555ebe532
Create Date: 2026-04-30 19:03:12.448991

"""
import json
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a33acbd380c9'
down_revision: Union[str, None] = '517555ebe532'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 마이그레이션 시점의 default config 를 명시적으로 박아둠.
# (이후 코드에서 DEFAULT_FORM_CONFIG 가 바뀌어도 마이그레이션 결과는 일관)
_V0_CONFIG = {
    "welcome_message": None,
    "questions": [
        {
            "type": "long_text",
            "id": "default_motivation",
            "label": "Why do you want to work here?",
            "required": True,
            "max_length": 1000,
            "placeholder": None,
        },
        {
            "type": "long_text",
            "id": "default_anything_else",
            "label": "Anything else you'd like us to know?",
            "required": False,
            "max_length": 1000,
            "placeholder": None,
        },
    ],
    "attachments": [
        {
            "id": "default_resume",
            "label": "Resume (optional)",
            "description": "PDF or image. Up to 20 MB.",
            "accept": "pdf_or_image",
            "required": False,
        },
    ],
}


def upgrade() -> None:
    bind = op.get_bind()
    # published form 이 하나도 없는 매장 찾기
    stores_without_form = bind.execute(
        sa.text(
            """
            SELECT s.id::text
            FROM stores s
            WHERE s.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM store_hiring_forms f
                  WHERE f.store_id = s.id
                    AND f.status = 'published'
              )
            """
        )
    ).fetchall()
    config_json = json.dumps(_V0_CONFIG)
    for (store_id,) in stores_without_form:
        bind.execute(
            sa.text(
                """
                INSERT INTO store_hiring_forms
                    (id, store_id, version, status, config, is_current, created_at, updated_at)
                VALUES
                    (:id, :store_id, 0, 'published', CAST(:config AS jsonb), TRUE, NOW(), NOW())
                """
            ),
            {"id": str(uuid.uuid4()), "store_id": store_id, "config": config_json},
        )


def downgrade() -> None:
    # version=0 인 published row 만 삭제 (사용자가 직접 만든 v1+ 은 보존)
    op.execute(
        "DELETE FROM store_hiring_forms WHERE version = 0 AND status = 'published'"
    )
