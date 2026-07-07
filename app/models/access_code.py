"""범용 Access Code 테이블 — 서비스별·조직별 외부 접근 제한용 코드.

Generic access code registry for gating external service endpoints.
각 row는 `(service_key, organization_id)` 로 식별된다 — 즉 서비스별로 조직마다
별도 코드를 가진다. `code` 값은 `service_key` 안에서 전역 유니크이므로,
제출된 코드 하나만으로 조직을 역조회할 수 있다 (태블릿 등록 시 회사코드 불필요).

Current consumers:
    - "attendance": Attendance Device 등록 시 입력하는 코드 (조직별)

소스 동작:
    - `source="env"`: 서버 기동 시 환경 변수에서 읽어 upsert (단일 org 하위호환)
    - `source="auto"`: env 미설정 시 서버가 랜덤 6자 생성 (조직당 최초 1회 INSERT)

관리자가 admin 엔드포인트로 수동 rotate 가능 (자기 조직 코드만).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AccessCode(Base):
    """서비스 접근 제한용 코드 ((service_key, organization_id)별 1 row).

    Attributes:
        id: 내부 식별자 (Internal identifier)
        service_key: 서비스 키 (예: "attendance")
        organization_id: 소속 조직 FK — 조직별 코드. (nullable: org 무관 전역 서비스 대비)
        code: 현재 유효한 코드 (숫자/영숫자 6자). service_key 내 전역 유니크.
        source: "env" | "auto"
        rotated_at: 최근 rotate 시각 (nullable)
        created_at: 최초 생성 시각
    """

    __tablename__ = "access_codes"
    __table_args__ = (
        # 조직당 서비스별 코드 1개
        UniqueConstraint("service_key", "organization_id", name="uq_access_code_service_org"),
        # 코드 → 조직 역조회 유니크 (service_key 안에서 code 는 유일)
        UniqueConstraint("service_key", "code", name="uq_access_code_service_code"),
    )

    # 내부 식별자 — Internal PK
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 서비스 키 — 서비스별 식별자. 예: "attendance"
    service_key: Mapped[str] = mapped_column(String(50), nullable=False)
    # 소속 조직 FK — 조직별 코드 (org 삭제 시 CASCADE). nullable: 전역 서비스 대비.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )
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
