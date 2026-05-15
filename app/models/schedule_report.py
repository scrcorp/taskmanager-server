"""스케줄 일일 리포트 스냅샷 모델.

매일 발송된 보고서의 이슈 목록을 JSONB로 저장. 다음 발송 시 set diff로
NEW/RESOLVED/ONGOING 분류에 사용한다.
"""

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Index, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScheduleReportSnapshot(Base):
    """일일 보고서 스냅샷. diff 비교 기준."""

    __tablename__ = "schedule_report_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_date_from: Mapped[date] = mapped_column(Date, nullable=False)
    target_date_to: Mapped[date] = mapped_column(Date, nullable=False)
    # [{"key": str, "category": str, "target_date": "YYYY-MM-DD", "label": str,
    #   "store_id": str?, "store_name": str?, "shift_id": str?, "shift_name": str?,
    #   "user_id": str?, "user_name": str?, "detail": dict?}]
    issues: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_schedule_report_snapshots_org_sent", "organization_id", "sent_at"),
    )
