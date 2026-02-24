"""평가 관련 SQLAlchemy ORM 모델 정의.

Evaluation SQLAlchemy ORM model definitions.
Includes evaluation templates (reusable scoring criteria),
template items (individual scoring items),
evaluations (actual evaluation records), and
evaluation responses (per-item scores/text).

Tables:
    - eval_templates: 평가 템플릿 (Evaluation templates)
    - eval_template_items: 평가 항목 (Items within a template)
    - evaluations: 평가 본체 (Evaluation records)
    - eval_responses: 평가 응답 (Per-item responses)
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, DateTime, Text, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class EvalTemplate(Base):
    """평가 템플릿 모델 — 재사용 가능한 평가 기준 템플릿.

    Evaluation template model — Reusable evaluation criteria template.
    Defines a set of scoring items for evaluating employees.

    Attributes:
        id: 고유 식별자 UUID
        organization_id: 소속 조직 FK
        name: 템플릿 이름
        target_role: 평가 대상 역할 (e.g. "staff", "supervisor")
        eval_type: 평가 유형 ("adhoc"=수시, "regular"=정기)
        cycle_weeks: 정기평가 주기 (주 단위, regular일 때 사용)
        created_at: 생성 일시 UTC
        updated_at: 수정 일시 UTC

    Relationships:
        items: 평가 항목 목록
    """

    __tablename__ = "eval_templates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    eval_type: Mapped[str] = mapped_column(String(20), default="adhoc")  # adhoc, regular
    cycle_weeks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    items = relationship("EvalTemplateItem", back_populates="template", cascade="all, delete-orphan", order_by="EvalTemplateItem.sort_order")


class EvalTemplateItem(Base):
    """평가 항목 모델 — 템플릿 내 개별 평가 기준.

    Evaluation template item model — Individual scoring criteria within a template.

    Attributes:
        id: 고유 식별자 UUID
        template_id: 소속 템플릿 FK
        title: 항목 제목
        type: 응답 유형 ("score"=점수, "text"=서술)
        max_score: 최대 점수 (score 유형일 때)
        sort_order: 정렬 순서
        created_at: 생성 일시 UTC
    """

    __tablename__ = "eval_template_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("eval_templates.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    type: Mapped[str] = mapped_column(String(20), default="score")  # score, text
    max_score: Mapped[int] = mapped_column(Integer, default=5)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    template = relationship("EvalTemplate", back_populates="items")


class Evaluation(Base):
    """평가 본체 모델 — 실제 수행된 평가 기록.

    Evaluation model — Actual evaluation record.
    Tracks who evaluated whom, using which template, and status.

    Direction rules:
        Owner → GM, GM → SV, SV → Staff (상위→하위 평가)

    Attributes:
        id: 고유 식별자 UUID
        organization_id: 소속 조직 FK
        store_id: 대상 매장 FK (optional)
        evaluator_id: 평가자 FK
        evaluatee_id: 피평가자 FK
        template_id: 사용 템플릿 FK (optional, 템플릿 삭제 시 null)
        status: 상태 ("draft"=작성중, "submitted"=제출됨)
        created_at: 생성 일시 UTC
        submitted_at: 제출 일시 UTC

    Relationships:
        responses: 평가 응답 목록
    """

    __tablename__ = "evaluations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    store_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True)
    evaluator_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    evaluatee_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("eval_templates.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft, submitted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    responses = relationship("EvalResponse", back_populates="evaluation", cascade="all, delete-orphan")


class EvalResponse(Base):
    """평가 응답 모델 — 평가 내 개별 항목에 대한 점수/서술 응답.

    Evaluation response model — Per-item score or text response.

    Attributes:
        id: 고유 식별자 UUID
        evaluation_id: 소속 평가 FK
        template_item_id: 대상 항목 FK
        score: 점수 (score 유형일 때)
        text: 서술 내용 (text 유형일 때)
        created_at: 생성 일시 UTC

    Constraints:
        uq_eval_response_eval_item: (evaluation_id, template_item_id) — 항목당 1개 응답
    """

    __tablename__ = "eval_responses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    evaluation_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("evaluations.id", ondelete="CASCADE"), nullable=False)
    template_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("eval_template_items.id", ondelete="CASCADE"), nullable=False)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("evaluation_id", "template_item_id", name="uq_eval_response_eval_item"),
    )

    evaluation = relationship("Evaluation", back_populates="responses")
