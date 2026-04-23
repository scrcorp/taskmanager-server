"""Attendance Device 모델 — 매장 공용 근태 기기 (태블릿 등) 등록.

Attendance device registry — shared terminal devices at a store that
let multiple employees clock in/out with their personal PINs.

Lifecycle:
    1. POST /attendance/register  (access code) → row 생성, token_hash 저장, store_id=null
    2. PUT  /attendance/store     (token) → 매장 선택
    3. 기기에서 직원이 PIN 입력해 clock in/out
    4. 기기 또는 admin이 revoke → revoked_at 기록 (row 유지, 감사용)
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AttendanceDevice(Base):
    """매장 공용 근태 기기 등록 정보.

    Attributes:
        id: 기기 고유 식별자 (PK, UUID)
        organization_id: 소속 조직 FK (멀티테넌트 격리)
        store_id: 할당된 매장 FK (등록 직후는 null — 최초 setup에서 선택)
        device_name: 자동 배정 표시 이름 (예: "Terminal-A7K3")
        token_hash: 기기 인증 토큰의 sha256 해시 (평문 토큰은 저장하지 않음)
        fingerprint: 선택적 기기 지문 (user-agent, platform 등 식별 힌트)
        registered_at: 등록 시각
        last_seen_at: 마지막 인증 성공 시각
        revoked_at: 해제 시각 (null이면 활성)
    """

    __tablename__ = "attendance_devices"

    # 기기 고유 식별자 — Device PK (UUID v4, auto-generated)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — 조직 삭제 시 기기도 삭제
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # 할당된 매장 FK — 최초 등록 시 null, setup 후 채워짐. 매장 삭제 시 revoke 처리용으로 SET NULL
    store_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )
    # 자동 배정 기기 이름 — 예: "Terminal-A7K3" (admin이 rename 가능)
    device_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 토큰 해시 — sha256(token). 평문 토큰은 한 번만 반환, 이후 복구 불가.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # 기기 지문 — 최초 등록 시 클라이언트가 보낸 user-agent/platform 힌트 (nullable)
    fingerprint: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # 등록 시각
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    # 마지막 사용 시각 — 인증 요청마다 갱신
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 해제 시각 — null이면 활성. admin revoke 또는 기기 자체 unregister 시 채움.
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_attendance_devices_org_active", "organization_id", "revoked_at"),
        Index("ix_attendance_devices_store", "store_id"),
    )
