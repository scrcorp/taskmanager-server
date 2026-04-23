"""범용 Access Code 테이블 — 서비스별 외부 접근 제한용 코드.

Generic access code registry for gating external service endpoints.
Not tied to any specific feature — each row is keyed by `service_key`.

Current consumers:
    - "attendance": Attendance Device 등록 시 입력하는 코드

소스 동작:
    - `source="env"`: 서버 기동 시 환경 변수(ATTENDANCE_ACCESS_CODE 등)에서 읽어 upsert
    - `source="auto"`: env 미설정 시 서버가 랜덤 6자 생성 (첫 기동에만 INSERT)

관리자가 admin 엔드포인트로 수동 rotate 가능.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AccessCode(Base):
    """서비스 접근 제한용 코드 (service_key별 1 row).

    Attributes:
        id: 내부 식별자 (Internal identifier)
        service_key: 서비스 키 (unique, 예: "attendance")
        code: 현재 유효한 코드 (숫자/영숫자 6자)
        source: "env" | "auto"
        rotated_at: 최근 rotate 시각 (nullable)
        created_at: 최초 생성 시각
    """

    __tablename__ = "access_codes"

    # 내부 식별자 — Internal PK
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 서비스 키 — 서비스별 식별자 (unique). 예: "attendance"
    service_key: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    # 현재 유효 코드 — 6자 (plain text 저장. 짧고 회전 가능한 성격이라 hash 사용 안 함)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    # 소스 — "env" (환경변수로 주입) 또는 "auto" (서버 자동 생성)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    # 최근 rotate 시각 — Last rotation timestamp (null이면 최초 생성 이후 rotate 없음)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 생성 시각 — Initial creation timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
