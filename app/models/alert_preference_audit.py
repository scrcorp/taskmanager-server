"""Alert Preference Audit 모델 — 알림 수신 설정 변경 이력.

왜 필요한가:
    "그 알림 왜 확인 안 했냐" 는 분쟁에서 근거가 되는 기록이다.
    직원이 특정 카테고리의 푸시를 껐다면, **언제 껐는지**가 남아 있어야
    "그 시점에는 알림을 받지 않도록 본인이 설정해 둔 상태였다" 를 보일 수 있다.
    반대로 켜 둔 상태였다면 회사 쪽 전달 문제로 볼 근거가 된다.

설계:
    변경 1건 = **(카테고리, 채널) 하나의 값 변화** 한 행.
    JSON 통째로 스냅샷하지 않고 쪼개 두는 이유는 조회 목적이 명확하기 때문이다 —
    "2026-08-01 시점에 사용자 U 의 schedule/push 는 켜져 있었나?" 는
    그 시각 이전의 마지막 행 하나만 찾으면 답이 나온다.

    값은 3-상태다: True / False / None(미설정 = 기본값 따름).
    None 을 구분해야 "명시적으로 껐다" 와 "손댄 적 없다" 가 섞이지 않는다.

이력은 지우지 않는다 — 지워지면 증거로서 의미가 없다.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AlertPreferenceAudit(Base):
    """알림 선호 변경 1건 (카테고리 × 채널 단위).

    Attributes:
        id: 이력 고유 식별자 (PK, UUID)
        organization_id: 소속 조직 FK (멀티테넌트 격리)
        user_id: 설정의 주인 (알림을 받는 사람)
        changed_by_user_id: 실제로 변경한 사람. 본인이면 user_id 와 같다.
            관리자가 대신 바꾼 경우를 구분하기 위해 따로 둔다.
        category_code: alert_categories.CATEGORIES 의 code (schedule, warning, ...)
        channel: "in_app" | "email" | "push"
        old_value: 변경 전 값. None = 미설정(기본값 따름)
        new_value: 변경 후 값. None = 미설정으로 되돌림
        user_agent: 어떤 기기에서 바꿨는지 힌트 (진단용, 신뢰 대상 아님)
        changed_at: 변경 시각 (절대시각 — 실제로 일어난 사건이므로 TIMESTAMPTZ)
    """

    __tablename__ = "alert_preference_audits"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # 설정의 주인 — 이 사람에게 알림이 갈지 말지가 이 행의 주제다.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 실제 변경자 — 본인 변경이면 user_id 와 동일. 관리자 대행이면 다르다.
    changed_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    category_code: Mapped[str] = mapped_column(String(50), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)

    # 3-상태. None 은 "미설정(기본값 따름)" 으로 False 와 의미가 다르다.
    old_value: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    new_value: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    user_agent: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    # 핵심 조회: "사용자 U 의 (카테고리, 채널) 이 시각 T 에 어떤 값이었나"
    # → user_id + category_code + channel 로 좁히고 changed_at 역순 첫 행.
    __table_args__ = (
        Index(
            "ix_alert_pref_audits_lookup",
            "user_id", "category_code", "channel", "changed_at",
        ),
        Index("ix_alert_pref_audits_organization_id", "organization_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 디버깅 편의
        return (
            f"<AlertPreferenceAudit {self.category_code}.{self.channel} "
            f"{self.old_value}→{self.new_value} @{self.changed_at}>"
        )
