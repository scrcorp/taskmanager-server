"""이메일 인증코드 모델 — 6자리 인증코드 저장 및 검증.

Email verification code model — Stores 6-digit verification codes
for email verification during registration and post-login verification.

Tables:
    - email_verification_codes: 이메일 인증코드 (Email verification codes)
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Uuid, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EmailVerificationCode(Base):
    """이메일 인증코드 테이블.

    Email verification code table for managing email verification flow.
    Codes are 6-digit numbers with 5-minute expiry and brute-force protection.

    Attributes:
        id: 고유 식별자 (Primary key UUID)
        email: 인증 대상 이메일 (Target email address)
        code: 6자리 인증코드 (6-digit verification code)
        verification_token: 검증 성공 시 발급되는 토큰 (Token issued on successful verification)
        purpose: 용도 — "registration" | "login_verify" (Purpose of verification)
        attempts: 시도 횟수 (Number of verification attempts)
        max_attempts: 최대 시도 횟수 (Maximum allowed attempts)
        is_used: 사용 여부 (Whether the code has been used)
        expires_at: 만료 일시 (Expiration timestamp, 5 min after creation)
        created_at: 생성 일시 (Creation timestamp)
    """

    __tablename__ = "email_verification_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(6), nullable=False)
    verification_token: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, nullable=True
    )
    purpose: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="registration"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    is_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_evc_email_code", "email", "code"),
        Index("ix_evc_verification_token", "verification_token"),
        Index("ix_evc_expires_at", "expires_at"),
    )
