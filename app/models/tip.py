"""팁 관련 SQLAlchemy ORM 모델 정의.

Tip-related SQLAlchemy ORM model definitions.

Tables:
    - tip_entries: 직원 일별 팁 입력 (Daily tip entries per employee+store+work_role)
    - tip_distributions: 동료 분배 (Card-tip distributions to coworkers, 24h auto-accept)
    - tip_audit_logs: 변경 이력 (Audit trail for tip entries / distributions)

Design notes:
    - Card vs cash 계산 규칙 (변경 금지):
        Cash Tips Kept: 본인 입력값 그대로 (분배 차감 X)
        Card Tips: 본인 입력값 − Σ(분배 금액)
        Reported on 4070 = Cash + 신고용 카드팁
    - work_role 삭제는 SET NULL — historical entry 보존을 위해 work_role_name_snapshot 컬럼 유지.
    - audit_logs.entity_id 는 FK 없음 — entity 삭제돼도 로그 남기 위함.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    String,
    DateTime,
    Date,
    Text,
    Numeric,
    ForeignKey,
    UniqueConstraint,
    Index,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TipEntry(Base):
    """팁 입력 — 직원이 clock-out 시(attendance) 또는 staff app(직원 본인)에서 입력.

    운영 단위는 **schedule** 이다. 한 schedule 당 entry 한 건.
    schedule 이 없는 매니저 누락 추가(수기 입력)는 schedule_id NULL + store/work_role/date
    직접 지정으로 허용.

    Attributes:
        schedule_id: 연결된 schedule (있으면 운영 entry). SET NULL — schedule 삭제돼도 보존.
        store_id / work_role_id / date / work_role_name_snapshot:
            schedule_id 가 있으면 schedule 에서 derive 한 스냅샷.
            schedule 삭제 후에도 entry 가 어떤 매장·일자였는지 식별 가능.

    Constraints:
        uq_tip_entry_employee_schedule (partial, schedule_id NOT NULL):
            한 직원-schedule 조합당 entry 1건.
            schedule_id NULL (매니저 freeform) 은 uniqueness 검사 안 함 (수기 여러 건 가능).
    """

    __tablename__ = "tip_entries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    schedule_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    work_role_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("store_work_roles.id", ondelete="SET NULL"), nullable=True
    )
    work_role_name_snapshot: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    card_tips: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0"))
    cash_tips_kept: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0"))
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="staff_app")
    last_modified_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    last_modified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        # 같은 직원-schedule 조합당 1건 (partial: schedule_id NOT NULL 일 때만).
        Index(
            "uq_tip_entry_employee_schedule",
            "employee_id", "schedule_id",
            unique=True,
            postgresql_where="schedule_id IS NOT NULL",
        ),
        Index("ix_tip_entries_employee_date", "employee_id", "date"),
        Index("ix_tip_entries_store_date", "store_id", "date"),
    )


class TipDistribution(Base):
    """동료 분배 — 카드팁에서 동료에게 나눠준 내역. 24h 자동 수락.

    Attributes:
        id: 고유 식별자
        entry_id: 원본 entry FK (CASCADE — entry 삭제 시 분배도 삭제)
        receiver_id: 받는 직원 FK (SET NULL — 퇴사 대비)
        receiver_name_snapshot: 퇴사·삭제 대비 historical label
        amount: 분배 금액
        reason: 메모 (예: "Bar share", "Bus help")
        status: pending / accepted / auto_accepted
        pending_until: 자동 수락 시각 (등록 + 24h)
        accepted_at: 수락 시각 (수동/자동 무관)
    """

    __tablename__ = "tip_distributions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tip_entries.id", ondelete="CASCADE"), nullable=False
    )
    receiver_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    receiver_name_snapshot: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    pending_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_tip_distributions_receiver_status", "receiver_id", "status"),
        Index("ix_tip_distributions_pending_until", "pending_until"),
        Index("ix_tip_distributions_entry", "entry_id"),
    )


class Form4070Document(Base):
    """IRS Form 4070 사이클 폼.

    사이클 확정 시 직원별 1건 생성. signature_image_key 가 set 되면 SIGNED.
    pdf_key 는 weasyprint 로 생성된 PDF 의 storage key (Stage C 후반에 채움).

    Status:
        generated: 자동 생성됨 (서명 전)
        downloaded: 직원 또는 매니저가 다운로드함 (간단 tracking)
        signed: 직원이 서명 완료
    """

    __tablename__ = "form_4070_documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    period_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tip_periods.id", ondelete="CASCADE"), nullable=False
    )
    pdf_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    reported_cash: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0"))
    reported_card: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0"))
    paid_out: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0"))
    net_tips: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="generated")
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 레거시 서명 이미지 storage key — 이미 서명된 구 폼 호환용 (이행 대상).
    # 신규 서명은 signature_strokes(벡터)로 박제. resolve_url 로 런타임 변환.
    signature_image_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 벡터 서명 스냅샷 — {"strokes":[[[x,y]..]..],"aspect":w/h} (정규화 0..1).
    # 서명 순간 users.signature_strokes 를 박제. 나중에 유저 저장 서명이 바뀌어도 불변.
    # PDF 렌더는 strokes 우선, 없으면 signature_image_key(레거시) fallback.
    signature_strokes: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint("employee_id", "period_id", name="uq_form_employee_period"),
        Index("ix_form_period", "period_id"),
        Index("ix_form_employee_status", "employee_id", "status"),
    )


class TipPeriod(Base):
    """반월 사이클 단위 — 1-15 / 16-EOM. 매장별 분리.

    period 가 confirmed 면 안의 entry 는 수정 잠금. force-close 도 같은 confirmed.
    audit 는 tip_audit_logs 에 entity_type='tip_period' 으로 남긴다.

    Constraints:
        uq_tip_period_store_range: 같은 매장의 같은 (start, end) 한 행만.
    """

    __tablename__ = "tip_periods"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # force-close 사유 (override_reason). 일반 confirm 은 NULL.
    override_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "store_id", "start_date", "end_date",
            name="uq_tip_period_store_range",
        ),
        Index("ix_tip_periods_store_dates", "store_id", "start_date"),
    )


class TipAuditLog(Base):
    """팁 관련 변경 이력 — entity 삭제돼도 보존.

    Attributes:
        entity_type: tip_entry / tip_distribution
        entity_id: 대상 entity ID (FK 없음 — 삭제돼도 로그 유지)
        action: create / update / delete / accept / auto_accept
        actor_id: 수정한 사람 (SET NULL — 시스템 액션은 NULL)
        comment: 매니저 수정 시 필수 (사유)
        before/after: 변경 전후 스냅샷 (JSONB)
    """

    __tablename__ = "tip_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    before: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    after: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_tip_audit_entity", "entity_type", "entity_id"),
        Index("ix_tip_audit_actor_created", "actor_id", "created_at"),
    )
