"""경고 서명(WarningSignature) SQLAlchemy ORM 모델.

Warning Signature — 경고별 party(employee/manager)당 적용된 서명 1개.

설계 원칙:
    - 스냅샷: 적용 순간의 벡터 스트로크를 박제한다. 유저의 저장 서명
      (users.signature_strokes)을 나중에 바꿔도 과거 경고 서명은 불변
      (징계 기록 보존).
    - 신원: signer_user_id = 실제 서명한 본인. employee=warning.subject_user_id,
      manager=warning.issued_by_id 만 가능(service 강제). 대리 서명 금지(Owner 포함).
    - party 당 1개. unique(warning_id, party). 재서명은 strokes/시각 갱신(같은 행).

Tables:
    - warning_signatures: 경고×party 단위 적용 서명(스냅샷)
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WarningSignature(Base):
    """경고 서명 — 경고×party(employee|manager) 단위 적용 서명(벡터 스냅샷).

    Attributes:
        id: 고유 식별자 UUID
        warning_id: 대상 경고 FK (CASCADE — 경고 삭제 시 서명도 삭제)
        party: 서명 주체 구분 — 'employee'(대상 직원) | 'manager'(발행 매니저)
        signer_user_id: 실제 서명한 본인 FK (SET NULL). employee=subject, manager=issuer
        signed_at: 서명 일시 (UTC)
        method: 'drawn'(새로 그림) | 'saved'(저장 서명 재사용) — 감사용
        signature_strokes: 벡터 서명 스냅샷 {"strokes":[[[x,y]..]..],"aspect":w/h}
        created_at: 생성 일시 (UTC)

    Constraints:
        uq_warning_signature_party: (warning_id, party) 고유 — party 당 1개
    """

    __tablename__ = "warning_signatures"

    # 고유 식별자 — Unique identifier (UUID v4)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 대상 경고 FK — Warning (CASCADE: 경고 삭제 시 서명도 삭제)
    warning_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("warnings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 서명 주체 — 'employee'(대상 직원) | 'manager'(발행 매니저)
    party: Mapped[str] = mapped_column(String(20), nullable=False)
    # 서명 명의 — employee=subject_user_id, manager=issued_by_id (party 본인, service 강제)
    signer_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 캡처 계정 — 실제로 이 서명을 받은 로그인 계정/기기(감사). 온-디바이스(콘솔)에서
    # party 본인이 남의 세션 기기로 서명할 때 누구 계정에서 받았는지 기록. 셀프사인이면
    # signer_user_id 와 동일. SET NULL.
    captured_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 서명 일시 — Signed timestamp (UTC)
    signed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # 방식 — 'drawn'(새로 그림) | 'saved'(저장 서명 재사용)
    method: Mapped[str] = mapped_column(String(10), nullable=False, default="drawn")
    # 벡터 서명 스냅샷 — {"strokes":[[[x,y]..]..],"aspect":w/h} (적용 순간 박제)
    signature_strokes: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 생성 일시 — Record creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("warning_id", "party", name="uq_warning_signature_party"),
        Index("ix_warning_signatures_warning_id", "warning_id"),
    )
