"""리프레시 토큰 모델 — JWT 리프레시 토큰 저장.

Refresh Token model — Stores JWT refresh tokens for session management.
Each token is bound to a specific user and has an expiration timestamp.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RefreshToken(Base):
    """리프레시 토큰 테이블.

    Refresh token table for managing long-lived authentication sessions.

    Attributes:
        id: 고유 식별자 (Primary key UUID)
        user_id: 소유 사용자 ID (Owner user UUID)
        token: JWT 리프레시 토큰 문자열 (JWT refresh token string)
        expires_at: 만료 일시 (Expiration timestamp)
        created_at: 생성 일시 (Creation timestamp)
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    user = relationship("User", back_populates="refresh_tokens")
