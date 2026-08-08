"""normalize users.email to trimmed lowercase

users.email 은 사용자 입력 원본이 그대로 저장돼 왔는데, 이메일로 사용자를 찾는
코드는 전부 입력만 소문자화해서 원본 컬럼과 비교한다. 그래서 대문자가 섞인
주소로 가입한 계정은 (1) 이메일 중복 체크에 걸리지 않아 중복 계정이 생겼고
(2) 비밀번호 재설정/아이디 찾기에서 "계정 없음"이 됐다.

쓰기 경로는 app/utils/email_address.normalize_email 로 통일했고, 여기서는
기존 데이터를 canonical 형태(trim + 소문자)로 내린다.

중복이 새로 생기지는 않는다: lower(btrim(email)) 로 뭉쳤을 때 새로 겹치는 쌍이
있으면 아래 사전 검사에서 멈춘다(이미 대소문자까지 완전히 동일한 기존 중복은
그대로 둔다 — 계정 병합은 별도 결정 사항).

Revision ID: a7c3e91b4d20
Revises: 28f55a28c9f4
Create Date: 2026-08-07

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a7c3e91b4d20'
down_revision: Union[str, None] = '28f55a28c9f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # 사전 검사 — 정규화로 "새로" 생기는 충돌만 잡는다.
    # 이미 raw 값까지 동일한 중복은 이 마이그레이션이 만든 게 아니므로 통과시킨다.
    collisions = conn.exec_driver_sql(
        """
        SELECT lower(btrim(email)) AS norm,
               count(*)            AS total,
               count(DISTINCT email) AS distinct_raw
          FROM users
         WHERE email IS NOT NULL AND btrim(email) <> ''
         GROUP BY lower(btrim(email))
        HAVING count(*) > 1 AND count(DISTINCT email) > 1
        """
    ).fetchall()
    if collisions:
        detail = ", ".join(f"{r[0]} ({r[1]}행)" for r in collisions)
        raise RuntimeError(
            "users.email 정규화 시 새로 충돌하는 주소가 있습니다. "
            "계정 정리 후 다시 시도하세요: " + detail
        )

    op.execute(
        """
        UPDATE users
           SET email = lower(btrim(email))
         WHERE email IS NOT NULL
           AND email <> lower(btrim(email))
        """
    )


def downgrade() -> None:
    # 원본 대소문자는 보존되지 않으므로 되돌릴 수 없다 (canonical 로만 저장).
    pass
