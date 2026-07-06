"""조직 소속(org_member) 및 소속-매장 배정(org_member_stores) 모델.

Model B (전역 정체성) 관계 테이블. 한 사람(users)이 여러 org 에 소속될 수 있고,
org 별 속성(role·시급·사번·PIN·재직상태)은 users 가 아니라 이 org_members 에 담긴다.
"membership" 은 org↔플랫폼 구독(license)과 혼동되어 폐기 → org_members 로 명명.

Tables:
    - org_members: user × org 소속 (org 별 role/시급/사번/PIN/status)
    - org_member_stores: org_member × store 매장 배정 (기존 user_stores 대체)
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# 소속 상태 — org 별 재직 상태. 계정 전체 상태(users.status)와 별개 층.
ORG_MEMBER_STATUSES = ("active", "on_leave", "terminated")


class OrgMember(Base):
    """조직 소속 — 한 사람이 특정 org 에서 갖는 role·시급·사번·PIN·재직상태.

    Model B 의 핵심 관계행. (user, org) 당 1행.
    같은 user 가 2개 org 소속이면 org_member 행 2개(독립 role/시급/status).

    Attributes:
        id: 고유 식별자 (Primary key UUID)
        user_id: 전역 계정 FK (Global user account)
        organization_id: 소속 조직 FK (Organization)
        role_id: 이 org 에서의 역할 FK (Role within this org — org-scoped)
        hourly_rate: 이 org 에서의 기본 시급 (nullable = org 기본값 사용)
        department: FOH/BOH 근무구역 (nullable)
        clockin_pin: 근태 기기 PIN (org 내 unique, NULL 다중 허용)
        employee_no: 사번 (org 내 non-null unique)
        status: 재직 상태 (active/on_leave/terminated)
        created_at/updated_at: 타임스탬프 (UTC)

    Constraints:
        uq_org_member_user_org: (user, org) 당 1행
        uq_org_member_clockin_pin: org 내 PIN unique (partial, NOT NULL)
        uq_org_member_employee_no: org 내 사번 unique (partial, NOT NULL)
    """

    __tablename__ = "org_members"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 전역 계정 FK — 계정 하드 purge(관리자 명시) 시에만 삭제. 소프트 삭제(status)는 행 유지.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 소속 조직 FK — 조직 삭제 시 소속도 삭제
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 이 org 에서의 역할 FK — 역할 삭제는 제한(RESTRICT)
    role_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("roles.id"), nullable=False)
    # 이 org 에서의 기본 시급 (nullable = org 기본값)
    hourly_rate: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    # FOH/BOH 근무구역
    department: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 근태 기기 PIN — org 내 unique (NULL 다중 허용)
    clockin_pin: Mapped[str | None] = mapped_column(String(6), nullable=True)
    # 사번 — org 내 non-null unique
    employee_no: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 재직 상태 — org 별. 계정(users.status)과 별개.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    # org 번호(CREWID) — org 안에서 1부터 순번, org 내 unique. DB 컬럼명 = 라벨 = crewid.
    # (기존 employee_no[레거시 String]와 별개 — 새 정수 순번.)
    crewid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="uq_org_member_user_org"),
        Index(
            "uq_org_member_clockin_pin",
            "organization_id",
            "clockin_pin",
            unique=True,
            postgresql_where=text("clockin_pin IS NOT NULL"),
        ),
        Index(
            "uq_org_member_employee_no",
            "organization_id",
            "employee_no",
            unique=True,
            postgresql_where=text("employee_no IS NOT NULL"),
        ),
        Index(
            "uq_org_member_crewid",
            "organization_id",
            "crewid",
            unique=True,
            postgresql_where=text("crewid IS NOT NULL"),
        ),
    )

    # 관계 — Relationships
    user = relationship("User", foreign_keys=[user_id], back_populates="org_members")
    organization = relationship("Organization")
    role = relationship("Role")
    member_stores = relationship(
        "OrgMemberStore", back_populates="org_member", cascade="all, delete-orphan"
    )


class OrgMemberStore(Base):
    """소속-매장 배정 — org_member 가 그 org 안에서 배정된 매장.

    기존 user_stores 대체. user 에 직접 붙지 않고 org_member 에 매달린다
    (소속 삭제 시 매장배정도 함께 정리).

    Attributes:
        id: 고유 식별자
        org_member_id: 소속 FK (OrgMember)
        store_id: 매장 FK (Store — org_member 의 org 소속이어야 함)
        is_manager: 해당 매장 매니저 여부
        is_work_assignment: 해당 매장 근무배정 대상 여부
        created_at: 생성 일시
    """

    __tablename__ = "org_member_stores"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_member_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("org_members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_manager: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    is_work_assignment: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    # EMPID — 매장(store) 안에서 1부터 순번, store 내 unique. DB 컬럼명 = 라벨 = empid.
    # 사람이 매장에 배정될 때 그 매장의 다음 번호를 받는다(매장마다 독립).
    empid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("org_member_id", "store_id", name="uq_org_member_store"),
        Index(
            "uq_org_member_store_empid",
            "store_id",
            "empid",
            unique=True,
            postgresql_where=text("empid IS NOT NULL"),
        ),
    )

    org_member = relationship("OrgMember", back_populates="member_stores")
    store = relationship("Store")
