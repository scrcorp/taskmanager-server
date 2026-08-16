"""임포트 라벨 별칭 — payroll 파일의 매장/법인 표기를 우리 매장·그룹에 영구 매핑.

실제 상황: 같은 법인이 시스템마다 다른 이름을 쓴다 — 급여 마스터의 회사명
"M KOREAN BBQ", CFS 시트 코드 "ODG", 매장 표기 "MKB"/"MBK"(오탈자 포함)가
전부 우리 서비스의 그룹(MBQ+MSK) 하나를 가리킨다. 이 대응은 파일 1개의 사정이
아니라 **고정 사실**이므로, 업로드 때마다 다시 매핑하게 하지 않고 org 에 저장한다.

학습 경로: EMPID Import 의 매핑 패널에서 운영자가 라벨→매장(또는 단일매장 그룹)을
골라 재-preview 하면 그 매핑이 여기 upsert 된다. 이후 업로드는 자동 적용.
당회 업로드의 명시 매핑이 저장된 별칭보다 우선한다 (틀린 학습은 다시 골라 덮어씀).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ImportLabelAlias(Base):
    """정규화 라벨 1개 → 매장 또는 그룹. (org, key) 당 1행."""

    __tablename__ = "import_label_aliases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # 정규화 키 (_norm_key: 대문자 영숫자) — "MKB", "MKOREANBBQ", "ODG" 등
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    # 대상 — 둘 중 하나만 채워진다. 대상이 삭제되면 별칭도 함께 삭제(CASCADE) —
    # 죽은 매핑이 남아 미매칭 패널을 영영 가리는 것을 막는다.
    store_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=True
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("store_groups.id", ondelete="CASCADE"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "key", name="uq_import_label_alias_org_key"),
    )
