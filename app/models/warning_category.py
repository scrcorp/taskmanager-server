"""경고 사유 카테고리 — Warning category (org별 관리, v1.1).

v1 에서 코드 고정(frozenset)이던 사유 카테고리를 org별 DB 로 전환. admin 이
추가/숨김/삭제할 수 있고, org 생성 시 기본 12종이 시드된다(app.core.warning).

설계:
    - org-scope: organization_id 로 격리. UNIQUE(org, code) — 코드는 org 내 1개.
    - 3상태: active / hidden(is_hidden) / deleted(deleted_at, soft).
    - soft delete: deleted_at. **같은 code 재추가 시 새 row 가 아니라 이 row 를 revive**
      (deleted_at=NULL, is_hidden=False, label 갱신). UNIQUE(org,code) 가 이를 강제.
    - is_system: `other`(자유텍스트 사유 구동) — 항상 맨 끝, 숨김/삭제 불가.
    - 라벨 live 조회: 경고는 코드만 저장하고, 표시 라벨은 이 테이블에서 resolve
      → 이름 변경이 과거 경고에도 반영(같은 개념).

Tables:
    - warning_categories: org별 사유 카테고리 (code + label + 정렬 + 숨김/시스템 + soft delete)
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WarningCategory(Base):
    """경고 사유 카테고리 — org별 관리 가능한 사유 항목.

    Attributes:
        id: 고유 식별자 UUID
        organization_id: 소속 조직 FK (CASCADE)
        code: 사유 코드 슬러그 (org 내 UNIQUE). 경고의 categories[] 가 이 코드를 참조.
        label: 표시 라벨 (영문). 변경 시 과거 경고 표시에도 live 반영.
        sort_order: 정렬 순서 (작을수록 먼저). system(other)은 항상 큰 값(맨 끝).
        is_hidden: 숨김 여부 (picker/목록에서 제외, 관리화면에선 'Hidden' 으로 표시).
        is_system: 시스템 카테고리(other) — 숨김/삭제 불가, 항상 맨 끝.
        deleted_at: 소프트 삭제 일시 (NULL = 살아있음). 같은 code 재추가 시 revive.
        created_at / updated_at: 타임스탬프 (UTC)

    Constraints:
        uq_warning_category_org_code: 조직 내 code 고유 (revive 보장 — 삭제돼도 row 유지)
        ix_warning_categories_org_deleted: (organization_id, deleted_at) 조회 커버
    """

    __tablename__ = "warning_categories"

    # 고유 식별자 — Category unique identifier (UUID v4)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 소속 조직 FK — Organization scope (CASCADE)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 사유 코드 슬러그 — Reason code (org 내 unique, 경고가 참조)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    # 표시 라벨 — Display label (영문, live resolve)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    # 정렬 순서 — Sort order (system=other 은 항상 맨 끝)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # 숨김 — Hidden from picker/list (관리화면엔 표시)
    is_hidden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # 시스템 — System category (other): 숨김/삭제 불가, 항상 맨 끝
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # 소프트 삭제 일시 — Soft delete (NULL = alive). 같은 code 재추가 시 revive.
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 생성 일시 — Creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # 수정 일시 — Last modification timestamp (UTC, auto-updated)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "code", name="uq_warning_category_org_code"),
        Index("ix_warning_categories_org_deleted", "organization_id", "deleted_at"),
    )
