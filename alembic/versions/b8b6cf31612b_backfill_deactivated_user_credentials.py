"""backfill deactivated user credentials

Revision ID: b8b6cf31612b
Revises: 86e542e71dac
Create Date: 2026-08-07 19:40:29.177926

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8b6cf31612b'
down_revision: Union[str, None] = '86e542e71dac'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 비활성화(탈퇴 처리)된 실사용자의 org_member PIN 제거.
    # 비활성 사용자가 clockin PIN을 계속 보유하면 출퇴근 인증에 재사용될 수
    # 있고, 신규 PIN 발급 시 prefix 충돌 검사에 걸려 활성 사용자의 PIN
    # 선택지를 막기 때문에 반드시 NULL로 정리한다.
    # (is_provisional=true 유령 계정은 대상에서 제외 — 별도 라이프사이클)
    op.execute(
        """
        UPDATE org_members
        SET clockin_pin = NULL
        FROM users
        WHERE org_members.user_id = users.id
          AND org_members.organization_id = users.organization_id
          AND users.is_active = false
          AND users.is_provisional = false
          AND org_members.clockin_pin IS NOT NULL
        """
    )

    # 비활성화된 실사용자의 users 레벨 자격 정보 정리.
    # - clockin_pin: org_members와 동일한 이유로 제거
    # - email_verified = false: 비활성 계정의 이메일 인증 상태를 남겨두면
    #   재가입/이메일 재사용 시 인증을 건너뛰는 경로가 생기므로 초기화
    # - status = 'deactivated': is_active 불리언과 status 문자열이 어긋난
    #   기존 행들을 단일 상태값으로 정합화
    op.execute(
        """
        UPDATE users
        SET clockin_pin = NULL,
            email_verified = false,
            status = 'deactivated'
        WHERE is_active = false
          AND is_provisional = false
        """
    )

    # access_codes.source 'env' 값 폐기.
    # ENV 기반 액세스 코드 공급 경로가 제거되어 'env' source는 더 이상
    # 유효하지 않으므로, 남아 있는 행을 'auto'(시스템 발급)로 이관한다.
    op.execute(
        """
        UPDATE access_codes
        SET source = 'auto'
        WHERE source = 'env'
        """
    )


def downgrade() -> None:
    # 데이터 백필은 원본 값(PIN, 인증 상태, source)을 보존하지 않으므로
    # 비가역이다. 되돌리지 않는다.
    pass
