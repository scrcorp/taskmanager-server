"""휴직(on_leave) 처리 — 시작 / 복귀 / 예정일 자동 복귀. (D5)

휴직을 terminated 로 처리하면 근속 연수와 재고용 이력이 왜곡되고, active 로 두면
스케줄 후보에 계속 뜬다. 그래서 `org_members.status='on_leave'` 를 별도 상태로 쓴다.

**availability 는 지우지 않는다.** 휴직은 한시적이므로 근무가능시간을 지우면 복귀할 때
직원이 전부 다시 입력해야 한다. 스케줄 후보에서 제외하는 것만으로 목적이 달성된다.

설계: docs/99_inbox/2026-08-13-조직계층-재정의.md §6 · D5
"""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org_member import OrgMember
from app.models.schedule import Schedule
from app.models.user import User
from app.core.error_codes.employment import (
    ALREADY_TERMINATED,
    EMPLOYEE_NOT_FOUND,
    LEAVE_END_BEFORE_START,
    MEMBERSHIP_NOT_FOUND,
    NOT_ON_LEAVE,
)

# 휴직 기간과 겹치는 미래 스케줄 처리.
# 퇴사(D3)와 달리 "유지"가 정당한 선택지다 — 짧은 휴직이면 복귀 후 그대로 근무한다.
# 다만 기본값은 두지 않는다: 무엇을 할지 매번 명시하게 한다.
LeaveScheduleAction = Literal["unassign", "delete", "keep"]


class LeaveService:
    """휴직 시작·복귀 단일 진입점."""

    async def start_leave(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
        *,
        start_date: date,
        schedule_action: LeaveScheduleAction,
        end_date: date | None = None,
        leave_type: str | None = None,
        is_paid: bool | None = None,
        note: str | None = None,
    ) -> dict[str, int | str | None]:
        """휴직을 시작한다.

        계정은 살려둔다 — 휴직자는 앱으로 자기 기록을 보고 복귀 안내를 받아야 한다.
        스케줄 후보에서 빠지는 것은 `status='on_leave'` 로 처리된다.

        Raises:
            EMPLOYEE_NOT_FOUND / MEMBERSHIP_NOT_FOUND: 사용자 또는 org 소속이 없을 때
            LEAVE_END_BEFORE_START / ALREADY_TERMINATED: 상태 전이가 성립하지 않을 때
        """
        if end_date is not None and end_date < start_date:
            raise LEAVE_END_BEFORE_START()

        member, _user = await self._load(db, user_id, organization_id)
        if member.status == "terminated":
            raise ALREADY_TERMINATED()

        try:
            member.status = "on_leave"
            member.leave_start_date = start_date
            member.leave_end_date = end_date
            member.leave_type = leave_type
            member.leave_is_paid = is_paid
            member.leave_note = note
            await db.flush()

            affected = 0
            if schedule_action != "keep":
                affected = await self._handle_leave_schedules(
                    db,
                    user_id=user_id,
                    organization_id=organization_id,
                    start_date=start_date,
                    end_date=end_date,
                    action=schedule_action,
                )
            await db.commit()
            return {
                "status": "on_leave",
                "leave_start_date": start_date.isoformat(),
                "leave_end_date": end_date.isoformat() if end_date else None,
                "schedule_action": schedule_action,
                "schedules_affected": affected,
            }
        except Exception:
            await db.rollback()
            raise

    async def end_leave(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
    ) -> dict[str, str]:
        """휴직을 종료하고 복귀시킨다 (수동).

        휴직 기록(시작일/분류 등)은 지우지 않는다 — 이력이다.
        """
        member, _user = await self._load(db, user_id, organization_id)
        if member.status != "on_leave":
            raise NOT_ON_LEAVE()
        try:
            member.status = "active"
            await db.commit()
            return {"status": "active"}
        except Exception:
            await db.rollback()
            raise

    async def apply_due_returns(
        self,
        db: AsyncSession,
        organization_id: UUID,
        *,
        today: date,
    ) -> int:
        """복귀 예정일이 지난 휴직자를 자동 복귀시킨다. 반환값 = 복귀 인원.

        멱등하다 — 이미 active 면 대상이 아니다. 일 단위 배치에서 호출하는 것을 전제로 하되,
        배치가 하루 걸러도 다음 실행에서 따라잡는다(경과분 전부 대상).
        """
        result = await db.execute(
            sa_update(OrgMember)
            .where(
                OrgMember.organization_id == organization_id,
                OrgMember.status == "on_leave",
                OrgMember.leave_end_date.is_not(None),
                OrgMember.leave_end_date <= today,
            )
            .values(status="active")
        )
        await db.commit()
        return int(result.rowcount or 0)

    async def _load(
        self, db: AsyncSession, user_id: UUID, organization_id: UUID
    ) -> tuple[OrgMember, User]:
        user: User | None = await db.scalar(
            select(User).where(
                User.id == user_id, User.organization_id == organization_id
            )
        )
        if user is None:
            raise EMPLOYEE_NOT_FOUND()
        member: OrgMember | None = await db.scalar(
            select(OrgMember).where(
                OrgMember.user_id == user_id,
                OrgMember.organization_id == organization_id,
            )
        )
        if member is None:
            raise MEMBERSHIP_NOT_FOUND()
        return member, user

    async def _handle_leave_schedules(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        organization_id: UUID,
        start_date: date,
        end_date: date | None,
        action: LeaveScheduleAction,
    ) -> int:
        """휴직 기간과 겹치는 스케줄 처리. end_date 가 없으면 시작일 이후 전부."""
        conditions = [
            Schedule.organization_id == organization_id,
            Schedule.user_id == user_id,
            Schedule.operating_day >= start_date,
            Schedule.status.in_(["confirmed", "requested"]),
        ]
        if end_date is not None:
            conditions.append(Schedule.operating_day <= end_date)
        values = {"user_id": None} if action == "unassign" else {"status": "deleted"}
        result = await db.execute(sa_update(Schedule).where(*conditions).values(**values))
        await db.flush()
        return int(result.rowcount or 0)


leave_service = LeaveService()
