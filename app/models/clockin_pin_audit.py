"""Clock-in PIN 감사 로그 — 누가 누구의 PIN 을 보고 바꿨는지.

PIN 은 출퇴근 기록을 대신 찍을 수 있는 자격증명이다. 대리 출근 분쟁이 생겼을 때
"그 PIN 을 알 수 있었던 사람이 누구인가" 를 되짚을 근거가 이 테이블뿐이라,
조회(reveal)와 변경을 모두 남긴다.

기록 시점:
    - reveal:     HTMA manage 모드에서 평문 1건을 실제로 열어봤을 때
                  (목록 조회는 마스킹 상태라 대상 아님)
    - update:     매니저가 PIN 을 직접 지정
    - regenerate: 매니저가 랜덤 재발급

`device_id` 는 키오스크 경로일 때만 채워진다.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# action 값 — 문자열 상수로 고정 (Enum 타입은 마이그레이션 비용만 늘어남)
PIN_AUDIT_REVEAL = "reveal"
PIN_AUDIT_UPDATE = "update"
PIN_AUDIT_REGENERATE = "regenerate"

VALID_PIN_AUDIT_ACTIONS = frozenset(
    {PIN_AUDIT_REVEAL, PIN_AUDIT_UPDATE, PIN_AUDIT_REGENERATE}
)


class ClockinPinAudit(Base):
    """PIN 조회/변경 1건.

    Attributes:
        actor_user_id: 행위자 (매니저)
        target_user_id: 대상 직원
        action: reveal / update / regenerate
        device_id: 키오스크 경로일 때 그 기기. 콘솔 경로면 NULL
        store_id: 행위가 일어난 매장 (키오스크 세션의 매장). 콘솔 경로면 NULL
        meta: action 별 부가 정보 (schedule_audit_logs.diff / tip_audit_logs.before 와
              같은 자리). **PIN 값 자체는 절대 넣지 않는다** — 감사 로그가 자격증명
              사본이 되면 지키려던 것을 스스로 깨뜨린다. 길이·경로 같은 메타만.
    """

    __tablename__ = "clockin_pin_audits"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    target_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    device_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("attendance_devices.id", ondelete="SET NULL"), nullable=True
    )
    store_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )
    meta: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_clockin_pin_audits_target", "target_user_id", "created_at"),
        Index("ix_clockin_pin_audits_actor", "actor_user_id", "created_at"),
        Index("ix_clockin_pin_audits_org", "organization_id", "created_at"),
    )
