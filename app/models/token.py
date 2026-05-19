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
    Each token represents a specific device/client session.

    회전(rotation) 정책: refresh 호출 시 기존 row를 삭제하지 않고
    replaced_by_token/replaced_access_token/replaced_at 을 채워 둔다.
    grace window 안에 같은 R1 으로 다시 들어오면 캐시된 새 토큰을
    그대로 반환하여 멀티 탭/새로고침 race 를 멱등 처리한다.

    Attributes:
        id: 고유 식별자 (Primary key UUID)
        user_id: 소유 사용자 ID (Owner user UUID)
        token: JWT 리프레시 토큰 문자열 (JWT refresh token string)
        expires_at: 만료 일시 (Expiration timestamp)
        created_at: 생성 일시 (Creation timestamp)
        client_type: 클라이언트 유형 — "admin" | "app" (Client type)
        user_agent: User-Agent 원본 — 표시 시 파싱 (Raw UA, parsed at display time)
        ip_address: 마지막 접속 IP (Last known IP address, display only)
        last_used_at: 마지막 사용 시각 (Last activity timestamp)
        replaced_by_token: 회전 시 발급된 새 refresh token (멱등 응답용)
        replaced_access_token: 회전 시 발급된 새 access token (멱등 응답용)
        replaced_at: 회전 시각 — 이 값이 NULL 이면 활성 세션
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
    client_type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="unknown"
    )
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    replaced_by_token: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    replaced_access_token: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )
    replaced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    user = relationship("User", back_populates="refresh_tokens")
