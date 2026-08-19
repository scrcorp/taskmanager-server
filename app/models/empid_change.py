"""EMPID 변경 이력 — 누가/언제/어떤 번호를/어느 경로로 바꿨는지의 원장.

기존 EmployeeNoHistory 는 레거시 users.employee_no(폐기 방향) 전용이고, 신형
org_member_stores.empid 경로는 이력을 전혀 남기지 않았다 (2026-08-14 recon).
급여명세서·근태 export 가 empid 를 키로 쓰기 시작하므로, 번호가 바뀌면
"언제 누가 왜" 를 되짚을 수 있어야 한다 (조직계층 트랙 D21 의 이력 요건).

조회 UI 는 v1 에 없다 — 감사·복구용 DB 원장 (contacts 이력과 같은 방침).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# source 값 — 변경이 일어난 논리 경로
EMPID_SOURCE_COMMIT = "commit"       # EMPID Import/Edit/스태프 상세의 명시 기입·삭제
EMPID_SOURCE_RENUMBER = "renumber"   # commit 3-phase 의 잔여 인원 자동 재채번
EMPID_SOURCE_AUTO = "auto"           # 매장 배정 시 자동 채번 (ensure_member_store)
EMPID_SOURCE_ABSORB = "absorb"       # 유령(미가입) 흡수로 번호 이동
EMPID_SOURCE_CURSOR = "cursor"       # 커서(next_empid) 자체 변경 — 수동 조정·재계산


class EmpidChange(Base):
    """EMPID 변경 1건 — (사람, 매장) 의 old→new."""

    __tablename__ = "empid_changes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # 매장 삭제 시에도 이력은 남긴다 — 이력이 매장 FK 로 소멸하면 원장이 아니다
    store_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )
    store_name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 스냅샷
    # 계정 purge/익명화 후에도 행이 남도록 SET NULL + 이름 스냅샷
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    person_name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 스냅샷
    # source='cursor' 인 행은 사람이 아니라 커서 값의 old→new 를 담는다(user_id=NULL).
    old_empid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_empid: Mapped[int | None] = mapped_column(Integer, nullable=True)  # NULL=번호 삭제
    # 변경 사유 — 커서 수동 조정·재계산 적용은 필수, 그 외 경로는 선택.
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 논리 경로 (EMPID_SOURCE_*) — commit/renumber/auto/absorb
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    # 클라이언트 표면 — console/console_compact/htma/staff_app/backoffice/system/api
    # (app/core/client_surface). NULL=도입 전 레거시 행.
    channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 실행자 — 자동 채번·cron 등 시스템 동작은 NULL
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_empid_changes_org_created", "organization_id", "created_at"),
        Index("ix_empid_changes_store", "store_id"),
        Index("ix_empid_changes_user", "user_id"),
    )
