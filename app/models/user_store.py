"""사용자-매장 연결 모델 — 다대다 매핑 테이블.

User-Store association model — Many-to-many mapping table.
Links users to the stores they are assigned to within an organization.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserStore(Base):
    """사용자-매장 연결 테이블.

    User-Store association table for many-to-many relationships.

    Attributes:
        id: 고유 식별자 (Primary key UUID)
        user_id: 사용자 ID (User UUID)
        store_id: 매장 ID (Store UUID)
        created_at: 생성 일시 (Creation timestamp)
    """

    __tablename__ = "user_stores"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("stores.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_manager: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("user_id", "store_id", name="uq_user_store"),
    )

    # Relationships
    user = relationship("User", back_populates="user_stores")
    store = relationship("Store", back_populates="user_stores")
