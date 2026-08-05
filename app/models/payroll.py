"""Payroll v1 Phase 2 스키마 — pay_periods / payroll_entries / payroll_events.

급여 정산의 뼈대 3개 테이블 (스펙: docs/99_inbox/2026-08-03 payroll-v1-스키마-스펙.md §4/§5/§6):
    - pay_periods: 매장별 반월 정산 기간. 시스템 생성 전용(수동 생성 불가 → 겹침 원천 차단).
      'open' → 'confirmed'. store 는 RESTRICT — 확정 급여 기록이 매장 삭제로 소멸 금지.
    - payroll_entries: 확정 시 동결되는 직원별 스냅샷. breakdown(JSONB) 이 상세의
      유일 원천이고 스칼라 컬럼은 그 합계 (confirm 시 일치 검증).
    - payroll_events: penalty/OT 발생 이벤트 + 귀책 태그. grain = 일 단위
      (법적 부과 단위 — attendance 단위는 split shift 이중부과·주간 OT 표현불가로 폐기).

Design notes:
    - 계산 규칙(C1~C8)은 Phase 2 서비스에서 구현 — 이 모듈은 스키마만.
    - money 컬럼은 전부 Numeric(10,2) / Mapped[Decimal].
    - revision 은 amendment(Q3) 탈출구 — v1 은 항상 0.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PayPeriod(Base):
    """매장별 반월 정산 기간 — 시스템 생성 전용, 'open' → 'confirmed'.

    Attributes:
        organization_id: 소속 조직 FK (CASCADE)
        store_id: 매장 FK — RESTRICT (확정 급여 기록 보존, 매장 삭제로 소멸 금지)
        start_date / end_date: 반월 구간. 시스템 생성 전용 — 수동 생성 불가라
            겹침이 원천 차단됨 (겹침 CHECK 없음)
        status: 'open' → 'confirmed'
        confirmed_at / confirmed_by: 확정 시각/확정자 (SET NULL — 계정 삭제돼도 기록 보존)
        override_reason: force-confirm 사유 (TipPeriod 선례). 일반 confirm 은 NULL.

    Constraints:
        uq_pay_period_store_start: (store_id, start_date) UNIQUE
            — 같은 매장의 같은 시작일 기간은 한 행만.
    """

    __tablename__ = "pay_periods"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 확정 급여 기록 보존 — 매장 삭제로 소멸 금지 (RESTRICT)
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # force-confirm 사유. 일반 confirm 은 NULL.
    override_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint("store_id", "start_date", name="uq_pay_period_store_start"),
    )


class PayrollEntry(Base):
    """직원별 급여 스냅샷 — pay period confirm 시 동결.

    breakdown(JSONB) 이 상세의 유일 원천 (pydantic 계약 고정: rate 구간별
    {rate, regular/ot/dt minutes, amount} + 일별 {work_date, 분류별 minutes}
    + penalty 사유 목록 + tip_period_id). 스칼라 컬럼은 breakdown 합계이며
    confirm 시 일치 검증한다 (라운딩: 구간별 계산 후 합산, 최종 센트 반올림 1회).

    Attributes:
        pay_period_id: 소속 기간 FK (CASCADE — 기간 삭제 시 entry 도 삭제)
        user_id / org_member_id: SET NULL — attendance/schedules 관례 일치 (탈퇴 대비)
        empid / crewid: export 매칭 스냅샷. empid 소스는 org_member_stores
            (entry 의 store 기준). 둘 다 없으면 export 빈칸+경고.
        member_name: 이름 스냅샷 — 이름 조합 단일 헬퍼 경유
            (구조화 이름 있으면 조합, 없으면 full_name)
        revision: amendment(Q3) 탈출구 — v1 은 항상 0
        calc_version: breakdown 포맷 버전 — 동결된 과거 entry 렌더 보장

    Constraints:
        uq_payroll_entry_period_user_rev (partial, user_id NOT NULL):
            (pay_period_id, user_id, revision) UNIQUE — 기간당 직원별 revision 1건.
    """

    __tablename__ = "payroll_entries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    pay_period_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("pay_periods.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False
    )
    # attendance/schedules 관례 일치 — 유저 삭제돼도 entry(스냅샷) 보존
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    org_member_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("org_members.id", ondelete="SET NULL"), nullable=True
    )
    # export 매칭 스냅샷 (empid 소스 = org_member_stores, entry 의 store 기준)
    empid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    crewid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    member_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # amendment(Q3) 탈출구 — v1 은 항상 0
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    regular_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    ot_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    dt_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    regular_pay: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    ot_pay: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    dt_pay: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    penalty_pay: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    card_tips: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    gross_pay: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    # breakdown 포맷 버전 — 동결된 과거 entry 렌더 보장 (기본값 없음, 계산기가 명시)
    calc_version: Mapped[int] = mapped_column(Integer, nullable=False)
    breakdown: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
    )

    __table_args__ = (
        # 기간당 직원별 revision 1건 (partial: user_id NULL 인 고아 스냅샷은 검사 제외)
        Index(
            "uq_payroll_entry_period_user_rev",
            "pay_period_id", "user_id", "revision",
            unique=True,
            postgresql_where="user_id IS NOT NULL",
        ),
    )

    pay_period = relationship("PayPeriod")


class PayrollEvent(Base):
    """penalty/OT 발생 이벤트 + 귀책 태그 — grain = 일 단위.

    라이프사이클: open 기간 재계산마다 (org, user, work_date, kind) 키로 upsert
    (reason 갱신, attribution 은 보존). 조건 소멸 시 삭제 대신 voided_at 마킹
    (태깅 보존) — 집계는 voided_at IS NULL 만 본다.

    Attributes:
        attendance_id: 참고용 FK (dedup 키 아님, SET NULL)
        work_date: 발생 영업일 (store business-day label)
        kind: meal_penalty | rest_penalty | daily_ot | weekly_ot | seventh_day
        reason: 사람이 읽는 부과 사유 (C5) — 재감지 시 갱신
        attribution: staff | management (E1) — 재감지에도 보존
        tagged_by / tagged_at: 귀책 태그 기록자/시각
        voided_at: 조건 소멸 마킹 (삭제 대신)
        pay_period_id: confirm 시 부여 = 동결. 이후 변경은 amendment 에서만.

    Constraints:
        uq_payroll_event_user_date_kind (partial, user_id NOT NULL):
            (organization_id, user_id, work_date, kind) UNIQUE — 하루 1건/kind 스키마 보장.
    """

    __tablename__ = "payroll_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 참고용 — dedup 키 아님 (dedup 은 일 키 unique 가 담당)
    attendance_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("attendances.id", ondelete="SET NULL"), nullable=True
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    # meal_penalty | rest_penalty | daily_ot | weekly_ot | seventh_day
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # staff | management (E1). NULL = 미태깅.
    attribution: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    tagged_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    tagged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 조건 소멸 시 삭제 대신 마킹 (태깅 보존). 집계는 voided_at IS NULL 만.
    voided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # confirm 시 부여 = 동결. 기간 삭제 시 이벤트는 보존 (SET NULL).
    pay_period_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("pay_periods.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
    )

    __table_args__ = (
        # 하루 1건/kind — 일 단위 upsert 의 스키마 보장 (user NULL 고아 행은 제외)
        Index(
            "uq_payroll_event_user_date_kind",
            "organization_id", "user_id", "work_date", "kind",
            unique=True,
            postgresql_where="user_id IS NOT NULL",
        ),
        Index("ix_payroll_events_store_date", "store_id", "work_date"),
        Index("ix_payroll_events_user_date", "user_id", "work_date"),
    )
