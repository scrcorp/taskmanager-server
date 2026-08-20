"""시급 변경 이력(hourly_rate_history) 모델 — Payroll v1 Phase 1.

개인(org_member) 시급이 바뀔 때마다 append 되는 이력. 급여 계산의 유일한
rate 원천(rate_at resolver ①단)이 되는 근거 데이터라 org_member 삭제를
RESTRICT 로 막는다 (급여 근거 소멸 금지).

Design notes (스펙: docs/99_inbox/2026-08-03 payroll-v1-스키마-스펙.md §1):
    - old_rate NULL = 최초 기록 (이전 rate 없음).
    - 같은 (org_member, effective_date) 재등록은 기존 행 update — UNIQUE 보장.
    - store/org default rate 변경은 이력 미기록 (개인 rate 만 이력화, known limitation).
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    desc,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class HourlyRateHistory(Base):
    """개인 시급 변경 이력 — org_member 당 effective_date 별 1행.

    Attributes:
        organization_id: 소속 조직 FK (org 삭제 시 이력도 삭제)
        org_member_id: 대상 소속 FK — RESTRICT (급여 근거 데이터, 멤버 삭제로 소멸 금지)
        old_rate: 변경 전 시급 (NULL = 최초 기록)
        new_rate: 변경 후 시급 (> 0 CHECK)
        effective_date: 적용 시작일 (자유 날짜 — 과거/미래 허용, R4)
        reason: 변경 사유 (자유 텍스트)
        changed_by: 변경한 사람 FK (SET NULL — 계정 삭제돼도 이력 보존)

    Constraints:
        uq_rate_history_member_effective: (org_member_id, effective_date) UNIQUE
            — 같은 날 재등록은 insert 가 아니라 기존 행 update.
        ck_rate_history_new_rate_positive: new_rate > 0
    """

    __tablename__ = "hourly_rate_history"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 급여 근거 데이터 — 멤버 삭제로 소멸 금지 (RESTRICT)
    org_member_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("org_members.id", ondelete="RESTRICT"), nullable=False
    )
    # NULL = 최초 기록 (이전 rate 없음)
    old_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    new_rate: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    changed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint(
            "org_member_id", "effective_date",
            name="uq_rate_history_member_effective",
        ),
        CheckConstraint("new_rate > 0", name="ck_rate_history_new_rate_positive"),
        # rate_at resolver 조회용 — 멤버별 최신 이력 lookup (effective_date DESC)
        Index("ix_rate_history_member_effective", "org_member_id", desc("effective_date")),
    )

    org_member = relationship("OrgMember")


class BonusRateHistory(Base):
    """성과 보너스 가산율 변경 이력 — org_member_store 당 effective_date 별 1행.

    HourlyRateHistory 와 같은 규칙을 따른다. 다른 점은 스코프 하나뿐 —
    시급은 org_member(조직) 단위인데 보너스는 **매장별**이라 org_member_stores 에 매달린다
    (한 사람이 두 매장에서 다른 보너스를 받을 수 있다).

    보너스도 임금이라 "언제부터 얼마였는지"가 분쟁 시 근거가 된다. 시급과 규칙이
    같아야 운영이 단순해지므로 effective_date 는 급여기간 시작일(1일/16일)만 허용한다
    (검증은 서비스 계층 — payroll_period_service.assert_period_start).

    Attributes:
        old_rate: 변경 전 가산율 (NULL = 최초 기록)
        new_rate: 변경 후 가산율 (>= 0 CHECK — 0 은 "보너스 없앰"이라 유효한 값)
    """

    __tablename__ = "bonus_rate_history"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 급여 근거 데이터 — 배정 삭제로 소멸 금지 (RESTRICT)
    org_member_store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("org_member_stores.id", ondelete="RESTRICT"), nullable=False
    )
    old_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    # 0 허용 — 시급과 달리 "보너스를 없앤다"가 정상 변경이다.
    new_rate: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    changed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint(
            "org_member_store_id", "effective_date",
            name="uq_bonus_history_member_store_effective",
        ),
        CheckConstraint("new_rate >= 0", name="ck_bonus_history_new_rate_non_negative"),
        Index(
            "ix_bonus_history_member_store_effective",
            "org_member_store_id",
            desc("effective_date"),
        ),
    )

    org_member_store = relationship("OrgMemberStore")
