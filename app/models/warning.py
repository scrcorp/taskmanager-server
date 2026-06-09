"""경고(Warning) 관련 SQLAlchemy ORM 모델 정의.

Staff Warning v1 — 매니저(GM 이상)가 직원에 대해 발행하는 징계성 경고 기록.

설계 원칙:
    - org-scope: 모든 조회는 organization_id 로 격리.
    - 방향 검증: app.core.permissions.can_warn (발행자보다 엄격히 낮은 권한만 대상).
    - 다중 사유: categories = 종이 양식(warning_sample.pdf)의 12개 사유 코드 ARRAY.
    - soft delete: deleted_at. 읽기는 항상 deleted_at IS NULL.
    - 사람용 ID: seq (org당 1부터 증가). 표시 = "W-{seq:05d}".
    - level(심각도)은 v1 제외. 추후 nullable 컬럼으로 무리없이 편입 가능
      (카테고리/상태와 독립 축, 마이그레이션 1줄). 지금은 두지 않는다.

Tables:
    - warnings: 경고 본체 (대상 직원 + 발행자 + 매장 + 제목 + 사유[] + 상태)
"""

import uuid
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Warning(Base):
    """경고 본체 모델 — 직원에게 발행된 경고 기록.

    Direction rules (방향 검증):
        발행자는 엄격히 더 낮은 권한(더 큰 priority)인 직원만 경고 가능.
        Owner → GM/SV/Staff, GM → SV/Staff. 자기/동급 금지.
        (app.core.permissions.can_warn)

    Edit/Delete:
        Owner 는 조직 전체 수정/삭제. 그 외(GM)는 본인 발행건만(service 강제).
        삭제는 soft delete(deleted_at).

    Attributes:
        id: 고유 식별자 UUID
        organization_id: 소속 조직 FK (CASCADE)
        seq: 조직 내 일련번호 (UNIQUE(org, seq)). 표시 "W-{seq:05d}".
        issued_by_id: 발행자 FK (SET NULL, 작성 시 current user)
        subject_user_id: 대상 직원 FK (SET NULL)
        store_id: 대상 매장 FK (SET NULL) — 대상 직원의 소속 매장 중 하나
        title: 경고 제목/요약
        categories: 사유 코드 목록 (warning_sample.pdf 12종 중 다중)
        details: 사유 상세 서술 (free text)
        status: 'active'(유효) | 'withdrawn'(철회됨)
        warning_date: 발생/발행 일자 (date only)
        withdrawn_at: 철회 처리 일시 (status withdrawn 전환 시)
        deleted_at: 소프트 삭제 일시 (NULL = active)
        created_at: 생성 일시 UTC
        updated_at: 수정 일시 UTC

    Constraints:
        uq_warning_org_seq: 조직 내 seq 고유 (사람용 ID 충돌 방지)
        ix_warnings_org_deleted: (organization_id, deleted_at) — 상시 soft-delete + org 필터
    """

    __tablename__ = "warnings"

    # 경고 고유 식별자 — Warning unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope for multi-tenant isolation (CASCADE)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 조직 내 일련번호 — Per-org sequence for the human-readable id "W-{seq:05d}"
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # 발행자 FK — Issuer (set = current user at create, SET NULL on user delete)
    issued_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 대상 직원 FK — Subject (the warned staff member, SET NULL on user delete)
    subject_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 대상 매장 FK — Store the warning pertains to (SET NULL)
    store_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )
    # 제목/요약 — Short summary of the warning
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # 사유 코드 목록 — Reason category codes (warning_sample.pdf 12종, multi-select)
    categories: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False, default=list, server_default="{}"
    )
    # 사유 상세 — Details / description (free text)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 시정 조치 — Corrective action the employee must take (free text, PDF §2).
    # 종이 양식의 "corrective action" 칸. 미입력 시 PDF 에선 빈 줄로 표시.
    corrective_action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 상태 — 'active'(유효) | 'withdrawn'(철회됨, 기록 유지)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    # 발생/발행 일자 — Date the incident occurred / warning was issued (date only)
    warning_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 철회 일시 — Timestamp when status flipped to 'withdrawn' (잘못 발행 거둠)
    withdrawn_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 소프트 삭제 일시 — Timestamp when soft-deleted (NULL = active)
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

    __table_args__ = (
        UniqueConstraint("organization_id", "seq", name="uq_warning_org_seq"),
        Index("ix_warnings_organization_id", "organization_id"),
        Index("ix_warnings_subject_user_id", "subject_user_id"),
        # 상시 켜지는 soft-delete + org 필터 커버
        Index("ix_warnings_org_deleted", "organization_id", "deleted_at"),
    )
