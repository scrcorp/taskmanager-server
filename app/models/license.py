"""라이센스(license) 모델 — org 운영 자격.

org ↔ 플랫폼(우리)의 계약/구독. org 와 1:1. status='suspended' 이면 그 org 의
사용자는 접근이 차단된다(get_current_user 에서 강제). org 의 code 가 사실상 라이센스
핸들 역할(별도 키 없음). plan/expires_at 는 확장용(현재는 status 만 강제).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# 라이센스 상태 — active: 정상 / suspended: 운영자 정지(접근차단) / expired: 만료(접근차단)
LICENSE_STATUSES = ("active", "suspended", "expired")
# 플랜 — 현재는 라벨. 강제(모듈/시트)는 나중.
LICENSE_PLANS = ("trial", "starter", "growth", "enterprise")


class License(Base):
    """org 운영 자격. org 와 1:1.

    Attributes:
        id: 고유 식별자
        organization_id: org FK (unique — org당 1 라이센스)
        status: active / suspended / expired (active 아니면 접근 차단)
        plan: trial / starter / growth / enterprise (라벨)
        issued_at: 발급 일시
        expires_at: 만료 일시 (nullable — 무기한)
        created_at / updated_at
    """

    __tablename__ = "licenses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    plan: Mapped[str] = mapped_column(
        String(20), nullable=False, default="trial", server_default="trial"
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    organization = relationship("Organization")
