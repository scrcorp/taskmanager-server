"""사번 이력(ledger) 모델 — org별 사번 영구 burn 대장.

Employee number history (append-only ledger) model.
Within an organization, ANY employee number ever assigned is permanently
"burned": it is recorded here once and can NEVER be re-assigned to anyone
(including the original holder). This is decision Option A (permanent burn)
from docs/99_inbox/2026-06-29 사번-이력기반-유니크 + 네이밍 통일.md.

Tables:
    - employee_no_history: org별 사번 사용 이력 (Per-org employee number usage ledger)
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EmployeeNoHistory(Base):
    """사번 이력 대장 — append-only. (org_id, employee_no) 영구 유일.

    Append-only ledger of every employee number that has ever been assigned
    within an organization. Used to enforce permanent uniqueness ("burn"):
    a number present here cannot be re-used by anyone in that org.

    Attributes:
        id: 고유 식별자 (Primary key UUID)
        organization_id: 소속 조직 FK (Parent organization, CASCADE delete)
        employee_no: 사번 (Employee number, normalized — leading zeros preserved)
        first_assigned_user_id: 최초 부여 대상 유저 (Audit: first holder, nullable)
        created_at: 최초 기록 일시 UTC (When this number was first burned)

    Constraints:
        uq_emp_no_history_org_no: 조직 내 사번 유일 (Unique employee number per org)
    """

    __tablename__ = "employee_no_history"

    # 고유 식별자 — Unique identifier (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Parent organization (CASCADE: 조직 삭제 시 이력도 삭제)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # 사번 — Employee number (normalized 문자열, 선행0 보존)
    employee_no: Mapped[str] = mapped_column(String(50), nullable=False)
    # 최초 부여 대상 유저 — Audit only. 유저 하드삭제 시 NULL 로 보존 (이력 자체는 유지).
    first_assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 최초 기록 일시 — When this number was first burned (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "employee_no", name="uq_emp_no_history_org_no"),
        Index("ix_emp_no_history_org", "organization_id"),
    )
