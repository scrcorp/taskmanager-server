"""스케줄 관련 SQLAlchemy ORM 모델 정의.

Schedule-related SQLAlchemy ORM model definitions.

Tables:
    - schedules: 확정 스케줄 (Confirmed schedules — from request confirm or manual creation)
"""

import uuid
from datetime import date, datetime, time, timezone
from typing import Optional
from sqlalchemy import String, DateTime, Date, Time, Text, Boolean, Integer, Numeric, ForeignKey, UniqueConstraint, Index, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StoreWorkRole(Base):
    """매장 업무 역할 — shift+position 조합에 기본시간/휴식/체크리스트 통합."""

    __tablename__ = "store_work_roles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    shift_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("shifts.id", ondelete="CASCADE"), nullable=False)
    position_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("positions.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    default_start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    default_end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    break_start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    break_end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    # Headcount config — always stores all 8 keys: {"all": 3, "sun": 3, "mon": 3, ...}
    # use_per_day_headcount=false → use "all", true → use day keys
    headcount: Mapped[dict] = mapped_column(JSONB, nullable=False, default=lambda: {"all": 1, "sun": 1, "mon": 1, "tue": 1, "wed": 1, "thu": 1, "fri": 1, "sat": 1})
    use_per_day_headcount: Mapped[bool] = mapped_column(Boolean, default=False)
    default_checklist_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("checklist_templates.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("store_id", "shift_id", "position_id", name="uq_store_work_role"),
        Index("ix_store_work_roles_store", "store_id"),
    )


class StoreBreakRule(Base):
    """매장 휴게 규칙 — 매장당 1개."""

    __tablename__ = "store_break_rules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    store_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, unique=True)
    max_continuous_minutes: Mapped[int] = mapped_column(Integer, default=240)
    break_duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    max_daily_work_minutes: Mapped[int] = mapped_column(Integer, default=480)
    work_hour_calc_basis: Mapped[str] = mapped_column(String(20), default="per_store")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# ── 폐기된 신청(request) 모델 ────────────────────────────────
# ScheduleRequestTemplate / ScheduleRequestTemplateItem / ScheduleRequest 는
# 스케줄 신청 기능 폐기(2026-08-09)와 함께 제거했다.
# 신청 행 자체는 schedules(status='requested') 에 있었고, 위 테이블들은 그보다
# 앞선 세대의 잔존물이다. 테이블은 이력 보존을 위해 DB 에 남기고
# alembic/env.py LEGACY_TABLES 에 등록해 autogenerate 에서 제외한다.

class Schedule(Base):
    """통합 스케줄 — 신청/확정/거절 모든 상태를 포함.

    Status: draft / requested / confirmed / rejected / cancelled
    - draft: admin이 임시 저장한 스케줄 (제출 전)
    - requested: staff가 앱에서 신청하거나 admin이 pending으로 생성
    - confirmed: 확정된 근무 스케줄
    - rejected: 거절된 스케줄 (final, read-only)
    - cancelled: 취소된 스케줄 (confirmed 이후 취소, GM+ 만, final)
    """

    __tablename__ = "schedules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # Legacy FK — 구세대 schedule_requests 테이블을 가리킨다.
    # 그 테이블은 신청 기능 폐기(2026-08-09)로 모델에서 제거했지만 이력 보존을 위해
    # DB 에는 남아 있고 alembic/env.py LEGACY_TABLES 에 등록돼 있다.
    # 모델이 없으므로 ForeignKey 선언을 뗀다 — 선언을 남기면 SQLAlchemy 가
    # 참조 테이블을 못 찾아 매핑 자체가 실패한다. DB 제약은 그대로 유지된다.
    # 삭제 조건: LEGACY_TABLES 에서 두 테이블을 드롭할 때 이 컬럼도 함께 제거.
    request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    store_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True)
    work_role_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("store_work_roles.id", ondelete="SET NULL"), nullable=True)
    # Work Role snapshot — work role 이름/포지션이 변경/삭제되어도 스케줄 시점의 값을 보존
    work_role_name_snapshot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    position_snapshot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 영업일 귀속 라벨 — "이 근무가 표시/카운트되는 스케줄 날짜"(물리적 시각 아님, start_at이 그 역할).
    # Wave 3: 구 work_date 컬럼 제거 완료, operating_day가 유일한 라벨 (NOT NULL).
    operating_day: Mapped[date] = mapped_column(Date, nullable=False)
    # Wall-clock datetime encoding (naive local, interpreted in store tz). 각 시각이 자기 날짜를 가짐.
    # operating_day는 영업일 라벨이며 start_at의 날짜와 다를 수 있음(+1일, 자정 이후 근무).
    # Wave 3: 구 *_time 컬럼 제거 완료 — 아래 read-only 프로퍼티 shim이 API 응답 하위호환 제공.
    start_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)
    end_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)
    break_start_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)
    break_end_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)

    # ── 구 인코딩 read-only shim (Wave 3) ─────────────────────────────
    # 구 컬럼은 신 컬럼의 순수 투영이었으므로(work_date=operating_day, start_time=start_at.time())
    # 프로덕션의 옛 앱 빌드가 읽는 응답 필드를 계산으로 계속 방출한다 (D2 결정 — 강제 업데이트와 분리).
    # 쓰기는 불가(setter 없음) — 쓰려는 코드는 남은 Wave 3 미이행 지점이므로 즉시 드러난다.
    @property
    def work_date(self) -> date:
        return self.operating_day

    @property
    def start_time(self) -> Optional[time]:
        return self.start_at.time() if self.start_at is not None else None

    @property
    def end_time(self) -> Optional[time]:
        return self.end_at.time() if self.end_at is not None else None

    @property
    def break_start_time(self) -> Optional[time]:
        return self.break_start_at.time() if self.break_start_at is not None else None

    @property
    def break_end_time(self) -> Optional[time]:
        return self.break_end_at.time() if self.break_end_at is not None else None
    net_work_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Status: draft / requested / confirmed / rejected / cancelled
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    # Origin: 'manual' (사람이 등록한 스케줄) | 'walk_in' (출근 시 자동 생성된 워크인 스케줄)
    origin: Mapped[str] = mapped_column(String(20), nullable=False, server_default="manual", default="manual")
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 시급 — auto-filled from user > store > org cascade
    hourly_rate: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    # Request-specific fields (from merged schedule_requests)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_modified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Reject metadata
    rejected_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Cancel metadata (GM+ 만, confirmed → cancelled)
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Legacy modification history — 새 변경은 schedule_audit_logs를 사용. 데이터는 마이그레이션됨.
    modifications: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_schedules_org_store_opday", "organization_id", "store_id", "operating_day"),
        Index("ix_schedules_user_opday", "user_id", "operating_day"),
        Index("ix_schedules_status", "status"),
    )


class ScheduleAuditLog(Base):
    """스케줄 변경 이력 — Schedule의 모든 상태 변경/수정/취소 등을 audit trail로 기록.

    Event types: created, requested, modified, confirmed, rejected, cancelled, reverted, switched, deleted
    """

    __tablename__ = "schedule_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    schedule_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # diff: {field: {old, new}} — modifications 용
    diff: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # D9-5 — 확인하고 넘어간 경고. [{"code": ..., "params": {...}}, ...]
    # "누가 어떤 경고를 알고도 진행했나" 를 조회할 수 있어야 하므로 diff 에 섞지 않고
    # 별도 컬럼으로 둔다(기록 위치가 곧 조회 가능성).
    acknowledged_warnings: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_schedule_audit_logs_schedule_ts", "schedule_id", "timestamp"),
    )
