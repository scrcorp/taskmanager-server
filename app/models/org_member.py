"""조직 소속(org_member) 및 소속-매장 배정(org_member_stores) 모델.

Model B (전역 정체성) 관계 테이블. 한 사람(users)이 여러 org 에 소속될 수 있고,
org 별 속성(role·시급·사번·PIN·재직상태)은 users 가 아니라 이 org_members 에 담긴다.
"membership" 은 org↔플랫폼 구독(license)과 혼동되어 폐기 → org_members 로 명명.

Tables:
    - org_members: user × org 소속 (org 별 role/시급/사번/PIN/status)
    - org_member_stores: org_member × store 매장 배정 (기존 user_stores 대체)
"""

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
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

# empid 번호 구분 — 순번(커서에서 발급) vs 예외(대역 밖 수동 번호. 본사 이관 등).
# 커서 재계산은 'sequence' 만 본다 → 예외 번호가 순번을 끌어올리지 못한다.
# 기본값은 항상 'sequence'. 경로(자동/임포트/직접기입)로 추론하지 않는다(INV-6).
# empid 가 NULL 이면 이 값은 의미가 없다 — 판정에서 제외한다.
EMPID_KIND_SEQUENCE = "sequence"
EMPID_KIND_EXCEPTION = "exception"
EMPID_KINDS = (EMPID_KIND_SEQUENCE, EMPID_KIND_EXCEPTION)


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
    # 고용 시작/종료일 — Payroll v1 Phase 1. status 와 별개의 날짜 기록 (급여 기간 판정용).
    hire_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    termination_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 퇴사 사유 — Offboard 시 입력. 자유 텍스트(분류는 v1 에서 두지 않는다).
    termination_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 재고용 가능 여부 — NULL = 미판단. 재입사 검토 시 참고값.
    rehire_eligible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # ── 휴직(on_leave) — status='on_leave' 일 때만 의미가 있다 (D5) ──
    # 휴직을 terminated 로 처리하면 근속 연수·재고용 이력이 왜곡되므로 별도 상태로 둔다.
    leave_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 복귀 예정일 — NULL = 미정. 경과하면 apply_due_returns() 가 자동 복귀시킨다.
    leave_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 휴직 분류 코드 — 목록은 Organization 설정 `employment.leave_types` 가 소유.
    # 고객마다 분류가 다르므로 서버는 코드만 보관하고 값을 강제하지 않는다.
    leave_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 유급 여부 — NULL = 미지정
    leave_is_paid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    leave_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
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
    # empid 번호 구분 — sequence(순번) | exception(대역 밖 예외). EMPID_KIND_* 상수.
    # 커서 재계산이 예외를 제외하기 위한 분류값이다. empid 가 NULL 이면 무의미.
    empid_kind: Mapped[str] = mapped_column(
        String(20), nullable=False,
        default=EMPID_KIND_SEQUENCE, server_default=EMPID_KIND_SEQUENCE,
    )
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
