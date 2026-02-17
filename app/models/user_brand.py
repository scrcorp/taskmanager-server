"""사용자-브랜드 연결 모델 — 다대다 매핑 테이블.

User-Brand association model — Many-to-many mapping table.
Links users to the brands they are assigned to within an organization.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserBrand(Base):
    """사용자-브랜드 연결 테이블.

    User-Brand association table for many-to-many relationships.

    Attributes:
        id: 고유 식별자 (Primary key UUID)
        user_id: 사용자 ID (User UUID)
        brand_id: 브랜드 ID (Brand UUID)
        created_at: 생성 일시 (Creation timestamp)
    """

    __tablename__ = "user_brands"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    brand_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("brands.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("user_id", "brand_id", name="uq_user_brand"),
    )

    # Relationships
    user = relationship("User", back_populates="user_brands")
    brand = relationship("Brand", back_populates="user_brands")
