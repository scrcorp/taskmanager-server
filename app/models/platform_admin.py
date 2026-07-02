"""플랫폼 운영자(platform_admins) 모델.

Model B 관계 테이블 — 특정 users 계정이 "플랫폼 운영자(operator)" 자격을 가짐을 표시.
org 권한(RBAC)과 완전히 다른 층: 전 org 를 가로지르는 백오피스 god-mode 컨텍스트.
역할 구분 = 이 행의 존재 자체(별도 type 플래그 불필요).

로그인은 통합(일반 users 로그인)이지만, Platform 컨텍스트 진입/백오피스는
비밀경로 + step-up 재인증으로 보호. ENV 자격증명은 break-glass 로 잔존.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# operator 등급 — v1 은 'super' 단일. 확장(범위 제한 등)은 나중.
PLATFORM_ADMIN_LEVELS = ("super",)


class PlatformAdmin(Base):
    """플랫폼 운영자 자격 — user × 플랫폼.

    Attributes:
        id: 고유 식별자
        user_id: 전역 계정 FK (unique — 계정당 최대 1 operator 행)
        is_active: 활성 여부 (비활성 = operator 권한 정지)
        level: operator 등급 (v1='super', 확장 대비 컬럼)
        last_login_at: 마지막 백오피스 로그인 시각
        created_at: 생성 일시
    """

    __tablename__ = "platform_admins"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="super", server_default="super"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    user = relationship("User", foreign_keys=[user_id])
