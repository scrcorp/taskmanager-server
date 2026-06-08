"""평가(Evaluation) 관련 SQLAlchemy ORM 모델 정의.

Evaluation v1 redesign — JSONB/snapshot 기반 2-테이블 구조.

설계 원칙:
    - 평가 항목/척도는 정규화 테이블이 아닌 JSONB(config/template_snapshot)로 격리.
    - 템플릿 config shape 정의/검증은 app/core/evaluation.py.
    - 평가 작성 시 템플릿 config 를 evaluations.template_snapshot 으로 deep-copy 스냅샷
      → 이후 템플릿이 바뀌어도 과거 평가는 채점 기준이 고정된다.
    - 평가는 soft delete (deleted_at). 읽기는 항상 deleted_at IS NULL 필터.

Tables:
    - eval_templates: 평가 템플릿 (조직당 빌트인 Basic 1개, v2 빌더 대비 version/status 보유)
    - evaluations:   평가 본체 (피평가자 점수 + 코멘트 + 스냅샷)
"""

import uuid
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EvalTemplate(Base):
    """평가 템플릿 모델 — 조직별 평가 항목/척도 정의(JSONB config).

    v1 은 조직당 빌트인 Basic 1개만 시드한다(is_default=True).
    version/status/is_current/created_by_user_id 컬럼은 v2 템플릿 빌더(hiring
    StoreHiringForm 패턴)가 마이그레이션 없이 들어오도록 미리 둔 것. v1은 기본값만 쓴다.

    Attributes:
        id: 고유 식별자 UUID
        organization_id: 소속 조직 FK (CASCADE)
        name: 템플릿 이름 (Basic = "Basic Performance Evaluation")
        config: 평가 항목 + 척도 JSONB (shape: app/core/evaluation.EvalTemplateConfig)
        is_default: 조직 기본 템플릿 여부
        version: 조직 내 버전 번호 (UNIQUE(org, version) — v1 항상 1)
        status: 'published' | 'draft'
        is_current: 현재 활성 버전 여부
        created_by_user_id: 생성자 FK (SET NULL, 시드는 NULL)
        created_at: 생성 일시 UTC
        updated_at: 수정 일시 UTC

    Constraints:
        uq_eval_template_org_version: 조직 내 version 고유
    """

    __tablename__ = "eval_templates"

    # 템플릿 고유 식별자 — Template unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Parent organization (CASCADE: 조직 삭제 시 템플릿도 삭제)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 템플릿 이름 — Template display name
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 평가 항목 + 척도 — JSONB config (criteria[] + scale[]). shape: app/core/evaluation.py
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 조직 기본 템플릿 여부 — Whether this is the org's default template
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # 버전 번호 — Per-org version number (UNIQUE(org, version), v1 always 1)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # 상태 — 'published' | 'draft'
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="published", server_default="published"
    )
    # 현재 활성 버전 여부 — Whether this version is currently active
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # 생성자 FK — User who created this template (SET NULL, seed is NULL)
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "version", name="uq_eval_template_org_version"),
        Index("ix_eval_templates_organization_id", "organization_id"),
    )


class Evaluation(Base):
    """평가 본체 모델 — 실제 수행된 평가 기록.

    Direction rules (방향 검증):
        평가자는 엄격히 더 낮은 권한(더 큰 priority)만 평가 가능.
        Owner → GM/SV/Staff, GM → SV/Staff, SV → Staff. 자기평가/동급 금지.
        (app.core.permissions.can_evaluate)

    Snapshot:
        template_snapshot = 작성 시점 템플릿 config 의 deep-copy.
        job_title = 작성 시점 position 이름의 스냅샷.

    Attributes:
        id: 고유 식별자 UUID
        organization_id: 소속 조직 FK (CASCADE)
        evaluator_id: 평가자 FK (SET NULL, 작성 시 current user)
        evaluatee_id: 피평가자 FK (SET NULL)
        store_id: 대상 매장 FK (SET NULL)
        position_id: 대상 포지션 FK (SET NULL)
        job_title: 작성 시점 포지션 이름 스냅샷
        template_id: 사용 템플릿 FK (SET NULL)
        template_snapshot: 작성 시점 템플릿 config 스냅샷 JSONB
        period_start: 평가 기간 시작일
        period_end: 평가 기간 종료일
        responses: 항목별 점수 JSONB ({code: 1..max_score})
        improvement: 개선점 서술
        good_examples: 잘한 점 서술
        status: 'draft' | 'submitted'
        deleted_at: 소프트 삭제 일시 (NULL = active)
        created_at: 생성 일시 UTC
        updated_at: 수정 일시 UTC
        submitted_at: 제출 일시 UTC (status submitted 로 전환 시)

    Constraints:
        ix_evaluations_org_deleted: (organization_id, deleted_at) — 상시 soft-delete + org 필터
    """

    __tablename__ = "evaluations"

    # 평가 고유 식별자 — Evaluation unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope for multi-tenant isolation (CASCADE)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 평가자 FK — Evaluator (set = current user at create, SET NULL on user delete)
    evaluator_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 피평가자 FK — Evaluatee (SET NULL on user delete)
    evaluatee_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 대상 매장 FK — Store the evaluation pertains to (SET NULL)
    store_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )
    # 대상 포지션 FK — Position (SET NULL)
    position_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("positions.id", ondelete="SET NULL"), nullable=True
    )
    # 직책 스냅샷 — Snapshot of the position name at write time
    job_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # 사용 템플릿 FK — Template used (SET NULL on template delete)
    template_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("eval_templates.id", ondelete="SET NULL"), nullable=True
    )
    # 템플릿 스냅샷 — Deep-copy of template config at write time (shape: app/core/evaluation.py)
    template_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 평가 기간 시작일 — Evaluation period start (date only).
    # nullable: draft 는 기간 없이 저장 가능. submit 시에만 필수(service 강제).
    period_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # 평가 기간 종료일 — Evaluation period end (date only). draft 는 NULL 허용.
    period_end: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # 항목별 점수 — {code: int} JSONB ({criterion_code: 1..max_score})
    responses: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # 개선점 서술 — Areas for improvement (free text)
    improvement: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 잘한 점 서술 — Good examples (free text)
    good_examples: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 상태 — 'draft' | 'submitted'
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default="draft"
    )
    # 소프트 삭제 일시 — Timestamp when evaluation was soft-deleted (NULL = active)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    # 제출 일시 — Timestamp when status flipped to 'submitted'
    submitted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_evaluations_organization_id", "organization_id"),
        Index("ix_evaluations_evaluatee_id", "evaluatee_id"),
        # 상시 켜지는 soft-delete + org 필터 커버
        Index("ix_evaluations_org_deleted", "organization_id", "deleted_at"),
    )
