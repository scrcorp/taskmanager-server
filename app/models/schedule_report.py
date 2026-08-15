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
    # 재발송용 렌더 재료. 이게 없으면 "다시 보내기" 가 곧 "다시 만들기" 가 되는데,
    # 그 사이 시각이 흘러 있으므로 **내용이 달라진다** — 7시에 나갔어야 할 리포트를
    # 9시에 재생성하면 그건 다른 문서다. 그래서 만든 시점의 재료를 그대로 보관한다.
    # {"org_name", "sent_date", "target_dates", "yesterday", "stores", "cells",
    #  "diff": {"new": [...], "ongoing": [...], "resolved": [...]}}
    # 완성된 HTML/PDF 바이트가 아니라 재료를 담는다 — 결과물은 동일하게 재현되면서
    # 행당 수십 KB 로 유지되고, 나중에 디자인을 고치면 과거 건도 새 디자인으로 나온다.
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_schedule_report_snapshots_org_sent", "organization_id", "sent_at"),
    )
