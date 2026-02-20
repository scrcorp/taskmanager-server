"""근무 구성 관련 SQLAlchemy ORM 모델 정의.

Work configuration SQLAlchemy ORM model definitions.
Includes Shift (time-based work periods) and Position (job roles)
scoped under each Store.

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
    """근무 시간대 모델 — 매장별 근무 시간 구분.

    Shift model — Time-based work period definition per store.
    Examples: "오전" (morning), "오후" (afternoon), "야간" (night).

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        store_id: 소속 매장 FK (Parent store foreign key)
        name: 시간대 이름 (Shift name, e.g. "오전", "오후")
        sort_order: 정렬 순서 (Display order, lower = first)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Relationships:
        store: 소속 매장 (Parent store)

    Constraints:
        uq_shift_store_name: 매장 내 시간대 이름 고유 (Unique shift name per store)
    """

    __tablename__ = "shifts"

    # 시간대 고유 식별자 — Shift unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 매장 FK — Parent store (CASCADE: 매장 삭제 시 시간대도 삭제)
    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    # 시간대 이름 — Shift display name
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 정렬 순서 — Display sort order (0-based, lower = displayed first)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("store_id", "name", name="uq_shift_store_name"),
    )

    # 관계 — Relationships
    store = relationship("Store", back_populates="shifts")


class Position(Base):
    """포지션(직무) 모델 — 매장별 업무 포지션 정의.

    Position model — Job role/station definition per store.
    Examples: "그릴" (grill), "카운터" (counter), "청소" (cleaning).

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        store_id: 소속 매장 FK (Parent store foreign key)
        name: 포지션 이름 (Position name, e.g. "그릴", "카운터")
        sort_order: 정렬 순서 (Display order, lower = first)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Relationships:
        store: 소속 매장 (Parent store)

    Constraints:
        uq_position_store_name: 매장 내 포지션 이름 고유 (Unique position name per store)
    """

    __tablename__ = "positions"

    # 포지션 고유 식별자 — Position unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 매장 FK — Parent store (CASCADE: 매장 삭제 시 포지션도 삭제)
    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    # 포지션 이름 — Position display name
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 정렬 순서 — Display sort order (0-based, lower = displayed first)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("store_id", "name", name="uq_position_store_name"),
    )

    # 관계 — Relationships
    store = relationship("Store", back_populates="positions")
