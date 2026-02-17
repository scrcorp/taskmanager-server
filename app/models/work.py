"""근무 구성 관련 SQLAlchemy ORM 모델 정의.

Work configuration SQLAlchemy ORM model definitions.
Includes Shift (time-based work periods) and Position (job roles)
scoped under each Brand.

Tables:
    - shifts: 근무 시간대 (Work shifts, e.g. morning/afternoon/night)
    - positions: 포지션/직무 (Work positions, e.g. grill/cashier/cleaning)
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Integer, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Shift(Base):
    """근무 시간대 모델 — 브랜드별 근무 시간 구분.

    Shift model — Time-based work period definition per brand.
    Examples: "오전" (morning), "오후" (afternoon), "야간" (night).

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        brand_id: 소속 브랜드 FK (Parent brand foreign key)
        name: 시간대 이름 (Shift name, e.g. "오전", "오후")
        sort_order: 정렬 순서 (Display order, lower = first)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Relationships:
        brand: 소속 브랜드 (Parent brand)

    Constraints:
        uq_shift_brand_name: 브랜드 내 시간대 이름 고유 (Unique shift name per brand)
    """

    __tablename__ = "shifts"

    # 시간대 고유 식별자 — Shift unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 브랜드 FK — Parent brand (CASCADE: 브랜드 삭제 시 시간대도 삭제)
    brand_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False)
    # 시간대 이름 — Shift display name
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 정렬 순서 — Display sort order (0-based, lower = displayed first)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("brand_id", "name", name="uq_shift_brand_name"),
    )

    # 관계 — Relationships
    brand = relationship("Brand", back_populates="shifts")


class Position(Base):
    """포지션(직무) 모델 — 브랜드별 업무 포지션 정의.

    Position model — Job role/station definition per brand.
    Examples: "그릴" (grill), "카운터" (counter), "청소" (cleaning).

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        brand_id: 소속 브랜드 FK (Parent brand foreign key)
        name: 포지션 이름 (Position name, e.g. "그릴", "카운터")
        sort_order: 정렬 순서 (Display order, lower = first)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Relationships:
        brand: 소속 브랜드 (Parent brand)

    Constraints:
        uq_position_brand_name: 브랜드 내 포지션 이름 고유 (Unique position name per brand)
    """

    __tablename__ = "positions"

    # 포지션 고유 식별자 — Position unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 브랜드 FK — Parent brand (CASCADE: 브랜드 삭제 시 포지션도 삭제)
    brand_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False)
    # 포지션 이름 — Position display name
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 정렬 순서 — Display sort order (0-based, lower = displayed first)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("brand_id", "name", name="uq_position_brand_name"),
    )

    # 관계 — Relationships
    brand = relationship("Brand", back_populates="positions")
