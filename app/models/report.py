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

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid
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
    author_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    # 보고서 기준일. daily는 필수, issue는 선택일 수 있음 (nullable).
    report_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
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

    # daily 리포트는 store/date/period 유일성 유지 — payload->>'period' 부분 인덱스로 마이그레이션에서 처리.
    # 단순 UniqueConstraint는 payload 필드 참조 불가하므로 컬럼 unique는 두지 않는다.


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
