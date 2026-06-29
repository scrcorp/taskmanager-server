"""통합 Report 모델 (multi-type).

기존 daily_report.py를 대체할 신규 구조. type 디스크리미네이터로
daily/issue/... 등 모든 리포트 종류를 한 테이블에서 관리한다.

- 공통/필터용 필드는 정식 컬럼 (organization_id, store_id, status, report_date, ...)
- 타입별 본문/섹션 등 자유 필드는 payload JSONB
- comments는 별도 테이블 (CRUD 분리)
- templates도 type별 JSONB 페이로드

기존 daily_reports 테이블은 별도 PR에서 데이터 백필 후 제거 예정.
"""
import uuid
from datetime import datetime, date, timezone
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ReportTemplate(Base):
    """모든 리포트 타입의 통합 템플릿.

    type 필드로 daily/issue/... 구분.
    payload에 sections 등 타입별 구조 저장.
    """

    __tablename__ = "report_templates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )
    store_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 이 템플릿이 적용되는 report type code(예: daily의 'lunch'/'dinner') 배열.
    # null 또는 [] = 해당 type의 모든 report_type 에 적용(전체). 결정-9.
    applicable_types: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    # 타입별 자유 필드. e.g. daily: {"sections": [{"title", "description", "is_required", "sort_order"}, ...]}
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class Report(Base):
    """모든 리포트 타입의 통합 본문.

    공통 필드는 정식 컬럼, 타입별 본문은 payload JSONB.
    daily: {"period": "lunch"|"dinner", "sections": [{"title", "content", "sort_order", ...}]}
    issue: 추후 정의
    """

    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )
    template_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("report_templates.id", ondelete="SET NULL"), nullable=True
    )
    # 작성자. 컬럼은 모든 type 공유라 nullable 유지(issue/legacy 등). 단 daily는
    # per-person 유일성(결정-8)을 위해 서비스 레이어에서 NOT NULL 을 강제한다.
    author_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # daily status: draft | submitted | reviewed (P3 review 도입). 자유 문자열 유지.
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    # 보고서 기준일. daily는 필수, issue는 선택일 수 있음 (nullable).
    report_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 제출 마감 일시 (P2). store/report_type 의 deadline 설정으로부터 계산되어 저장.
    deadline_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 검토(review) 메타 (P3). reviewed_by_id 가 채워지면 검토 완료.
    reviewed_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 타입별 본문/섹션/메타데이터
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    comments = relationship(
        "ReportComment",
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="ReportComment.created_at",
    )
    acknowledgements = relationship(
        "ReportAcknowledgement",
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="ReportAcknowledgement.acknowledged_at",
    )

    # daily 리포트는 per-person 유일성 (결정-8): store + date + payload->>'period' + author_id.
    # payload 필드 참조 partial unique index 라 마이그레이션에서 처리(컬럼 unique 미사용).
    # 인덱스명: uq_reports_daily_store_date_period_author (WHERE type='daily' AND deleted_at IS NULL)


class ReportComment(Base):
    """리포트 댓글 (모든 타입 공통)."""

    __tablename__ = "report_comments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    report_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    report = relationship("Report", back_populates="comments")


class ReportType(Base):
    """리포트 타입(예: daily의 lunch/dinner/morning) 정의 — org-default + store override.

    결정-1/7/9. daily 리포트의 'period' 종류를 org/store별로 구성한다.

    Resolution (한 매장의 effective enabled types):
        - store_id IS NULL row = 조직 기본값(org-default).
        - store_id 지정 row = 같은 code 의 org row 를 override (활성/라벨/마감 재정의),
          또는 그 매장에만 존재하는 store-only type 추가.
        - effective = (active org rows  −  store 가 비활성화한 code)  +  store-added rows.

    Uniqueness (soft-delete 살아있는 row 한정, partial unique index 로 마이그레이션 처리):
        - org-default: (organization_id, code) WHERE store_id IS NULL
        - store override/add: (organization_id, store_id, code) WHERE store_id IS NOT NULL

    Seed (org 생성 시 / 기존 org 마이그레이션):
        lunch(is_active=true, sort 1), dinner(is_active=true, sort 2),
        morning(is_active=FALSE, sort 0)  — morning 은 존재하나 기본 off(결정-7).
    """

    __tablename__ = "report_types"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # NULL = org 기본값. 지정 시 같은 code 의 org row 를 override 하거나 store 전용 type.
    store_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=True
    )
    # 타입 코드 슬러그 (e.g. 'lunch', 'dinner', 'morning'). report payload->>'period' 가 참조.
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    # 표시 라벨 (영문). live resolve.
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # 기본 제출 마감 시각 "HH:MM" (매장 로컬 타임존 기준). NULL = 마감 없음.
    default_deadline_local_time: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    # 마감 기준일 오프셋(영업일 대비 +N일). 0 = 당일.
    deadline_day_offset: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 소프트 삭제
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        # org-default 행: (org, code) 유일 (살아있는 row 한정)
        Index(
            "uq_report_types_org_code",
            "organization_id",
            "code",
            unique=True,
            postgresql_where=text("store_id IS NULL AND deleted_at IS NULL"),
        ),
        # store override/add 행: (org, store, code) 유일 (살아있는 row 한정)
        Index(
            "uq_report_types_org_store_code",
            "organization_id",
            "store_id",
            "code",
            unique=True,
            postgresql_where=text("store_id IS NOT NULL AND deleted_at IS NULL"),
        ),
        Index("ix_report_types_org_store", "organization_id", "store_id"),
    )


class ReportAcknowledgement(Base):
    """리포트 확인(읽음 확인) — P3. 누가 언제 해당 리포트를 acknowledge 했는지."""

    __tablename__ = "report_acknowledgements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    report_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    acknowledged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    report = relationship("Report", back_populates="acknowledgements")

    __table_args__ = (
        UniqueConstraint("report_id", "user_id", name="uq_report_ack_report_user"),
    )
