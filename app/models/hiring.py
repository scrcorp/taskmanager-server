"""Hiring 도메인 모델 — 매장별 가입 폼, 후보자(Candidate), 지원(Application).

Hiring domain: per-store signup form, candidate (person), application (per-store submission).

설계 원칙:
- candidate = 사람 (한 번 가입하면 row 1개, 여러 매장 지원해도 동일 row)
- application = 한 매장 지원 1건 (후보자 × 매장 × 시도)
- 활성 application(new/reviewing/interview)은 후보자×매장당 1개로 제한
- 떨어지거나(rejected) 본인 철회(withdrawn)는 재지원 가능
- 모든 가변 구조는 JSONB로 격리. 스키마는 app/core/hiring.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.database import Base


class StoreHiringForm(Base):
    """매장 가입 폼의 한 버전. 매장당 여러 버전이 누적, is_current가 활성 1개."""

    __tablename__ = "store_hiring_forms"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("store_id", "version", name="uq_store_hiring_form_version"),
    )

    store = relationship("Store")
    applications = relationship("Application", back_populates="form")


class Candidate(Base):
    """공개 가입 링크로 들어온 사람 1명. 여러 매장 지원해도 row는 1개.

    hire 시 users 테이블로 정보가 이전되며 promoted_user_id로 연결된다.
    인증 식별은 username + email_normalized 둘 다 unique.
    phone은 optional — SMS 인증 없으니 정규화 안 함.
    """

    __tablename__ = "candidates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    # 사용자 입력 그대로(표시용)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    # lower(trim(email)) — 중복 체크용
    email_normalized: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # hire 시 생성된 user. 같은 candidate가 여러 매장에서 hire되어도 user 1개 + user_stores N개.
    promoted_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    promoted_user = relationship("User")
    applications = relationship("Application", back_populates="candidate")
    blocks = relationship("CandidateBlock", back_populates="candidate")


class Application(Base):
    """한 사람이 한 매장에 지원한 1건. 같은 매장 재지원이면 새 row(attempt_no 증가).

    활성 application(new/reviewing/interview)은 후보자×매장당 1개만 허용.
    rejected/withdrawn/hired 후엔 재지원 가능.
    """

    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    form_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("store_hiring_forms.id", ondelete="SET NULL"), nullable=True
    )
    # 같은 candidate × store에서의 시도 횟수 (1, 2, 3...). 재지원 시 +1.
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # 폼 답변 + 첨부 메타 (스냅샷). app/core/hiring.py의 ApplicantData로 검증.
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    stage: Mapped[str] = mapped_column(String(20), nullable=False, default="new", index=True)
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    interview_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        # 활성 단계에서만 unique — 떨어진 후 재지원 가능
        Index(
            "uq_active_application_per_store",
            "candidate_id",
            "store_id",
            unique=True,
            postgresql_where=text("stage IN ('new','reviewing','interview')"),
        ),
    )

    candidate = relationship("Candidate", back_populates="applications")
    store = relationship("Store")
    form = relationship("StoreHiringForm", back_populates="applications")


class CandidateBlock(Base):
    """매장이 후보자를 차단하는 기록. 일단 매장 단위만 (org 단위는 추후).

    같은 candidate × store에 대한 block은 1개만 (재차단 시 reason만 update).
    """

    __tablename__ = "candidate_blocks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    blocked_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("candidate_id", "store_id", name="uq_candidate_block_per_store"),
    )

    candidate = relationship("Candidate", back_populates="blocks")
    store = relationship("Store")
