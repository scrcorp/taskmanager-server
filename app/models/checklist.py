"""체크리스트 템플릿 관련 SQLAlchemy ORM 모델 정의.

Checklist template SQLAlchemy ORM model definitions.
Defines reusable checklist templates scoped to a specific
brand + shift + position combination, along with their items.

Tables:
    - checklist_templates: 체크리스트 템플릿 (Reusable checklist templates)
    - checklist_template_items: 체크리스트 항목 (Individual items within a template)
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Integer, Text, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ChecklistTemplate(Base):
    """체크리스트 템플릿 모델 — 브랜드/시간대/포지션 조합별 업무 체크리스트.

    Checklist template model — Reusable task checklist for a specific
    brand + shift + position combination. Only one template can exist
    per combination (enforced by unique constraint).

    When a WorkAssignment is created, the template's items are
    snapshot-copied into the assignment's JSONB checklist_snapshot field.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        brand_id: 소속 브랜드 FK (Brand foreign key)
        shift_id: 시간대 FK (Shift foreign key)
        position_id: 포지션 FK (Position foreign key)
        title: 템플릿 제목 (Template title)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Relationships:
        items: 체크리스트 항목 목록 (Template items, ordered by sort_order)

    Constraints:
        uq_template_brand_shift_position: 브랜드+시간대+포지션 조합 고유 (One template per combination)
    """

    __tablename__ = "checklist_templates"

    # 템플릿 고유 식별자 — Template unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 브랜드 FK — Brand scope (CASCADE: 브랜드 삭제 시 템플릿도 삭제)
    brand_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False)
    # 시간대 FK — Shift scope (CASCADE: 시간대 삭제 시 템플릿도 삭제)
    shift_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("shifts.id", ondelete="CASCADE"), nullable=False)
    # 포지션 FK — Position scope (CASCADE: 포지션 삭제 시 템플릿도 삭제)
    position_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("positions.id", ondelete="CASCADE"), nullable=False)
    # 템플릿 제목 — Template display title
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("brand_id", "shift_id", "position_id", name="uq_template_brand_shift_position"),
    )

    # 관계 — Items sorted by sort_order for consistent display ordering
    items = relationship("ChecklistTemplateItem", back_populates="template", cascade="all, delete-orphan", order_by="ChecklistTemplateItem.sort_order")


class ChecklistTemplateItem(Base):
    """체크리스트 템플릿 항목 모델 — 개별 체크리스트 작업 항목.

    Checklist template item model — Individual task item within a template.
    Items are ordered by sort_order and can require different verification types.

    Attributes:
        id: 고유 식별자 UUID (Unique identifier)
        template_id: 소속 템플릿 FK (Parent template foreign key)
        title: 항목 제목 (Item title/task description)
        description: 상세 설명 (Detailed description, optional)
        verification_type: 확인 유형 (Verification method: "none", "photo", "text")
        sort_order: 정렬 순서 (Display order, lower = first)
        created_at: 생성 일시 UTC (Creation timestamp)
        updated_at: 수정 일시 UTC (Last update timestamp)

    Relationships:
        template: 소속 템플릿 (Parent checklist template)
    """

    __tablename__ = "checklist_template_items"

    # 항목 고유 식별자 — Item unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 템플릿 FK — Parent template (CASCADE: 템플릿 삭제 시 항목도 삭제)
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("checklist_templates.id", ondelete="CASCADE"), nullable=False)
    # 항목 제목 — Task title/description (max 500 chars)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # 상세 설명 — Optional detailed instructions for the task
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 확인 유형 — Verification method: "none"=체크만, "photo"=사진첨부, "text"=텍스트입력
    verification_type: Mapped[str] = mapped_column(String(20), default="none")  # none, photo, text
    # 정렬 순서 — Display sort order (0-based, supports drag-and-drop reordering)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 관계 — Relationships
    template = relationship("ChecklistTemplate", back_populates="items")
