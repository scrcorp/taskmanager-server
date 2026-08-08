"""Access code 감사 로그 — 누가 조직의 기기 등록 코드를 바꿨는지.

Access code 는 기기(키오스크)를 조직에 귀속시키는 자격증명이다. 코드가 유출되어
낯선 기기가 등록되는 분쟁이 생겼을 때 "코드가 언제, 누구에 의해 바뀌었는가" 를
되짚을 근거가 이 테이블이다.

기록 시점:
    - manual_set: 운영자가 admin 엔드포인트로 코드를 직접 지정했을 때
    - rotate:     운영자가 랜덤 재발급(rotate)했을 때

GET 경로의 자동 생성(ensure)은 시스템 행위라 기록하지 않는다.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# action 값 — 문자열 상수로 고정 (Enum 타입은 마이그레이션 비용만 늘어남)
ACCESS_CODE_AUDIT_MANUAL_SET = "manual_set"
ACCESS_CODE_AUDIT_ROTATE = "rotate"

VALID_ACCESS_CODE_AUDIT_ACTIONS = frozenset(
    {ACCESS_CODE_AUDIT_MANUAL_SET, ACCESS_CODE_AUDIT_ROTATE}
)


class AccessCodeAudit(Base):
    """Access code 변경 1건.

    Attributes:
        organization_id: 코드가 속한 조직
        service_key: 어느 서비스 코드인가 (예: "attendance")
        actor_user_id: 행위자 (운영자). 유저 삭제 시 NULL 로 남겨 기록은 보존
        action: manual_set / rotate
        meta: action 별 부가 정보. **코드 값 자체는 절대 넣지 않는다** — 감사
              로그가 자격증명 사본이 되면 지키려던 것을 스스로 깨뜨린다.
              {"code_length": n} 같은 메타만.
    """

    __tablename__ = "access_code_audits"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    service_key: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    meta: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_access_code_audits_org", "organization_id", "created_at"),
    )
