"""퇴사(Offboard) 처리 — 흩어져 있던 부수효과를 한 경로로 모은다.

기존에는 "Delete" 버튼 하나가 비활성화 + 자격증명 회수만 했고, 미래 스케줄·재직 상태·
퇴사일은 아무도 건드리지 않았다. 그래서 퇴사 처리 후에도 그 사람이 다음 주 그리드에
남아 근태 대상·알림·급여 예측에 계속 등장했다.

설계: docs/99_inbox/2026-08-13-조직계층-재정의.md §6 · D3
"""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org_member import OrgMember
from app.models.schedule import Schedule
from app.models.user import Role, User
from app.core.error_codes.employment import (
    EMPLOYEE_NOT_FOUND,
    LAST_ADMIN,
    MEMBERSHIP_NOT_FOUND,
    OFFBOARD_PROVISIONAL,
)

# 미래 스케줄 처리 — D3. 기본값을 두지 않고 호출자가 매번 명시한다.
# "그대로 유지"는 선택지에 없다: 퇴사자가 근태 대상·알림·급여 예측에 계속 남고,
# PIN 회수 전이면 출근까지 찍힐 수 있다.
FutureScheduleAction = Literal["unassign", "delete"]


class OffboardingService:
    """퇴사 처리 단일 진입점."""

    async def offboard(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
        *,
        termination_date: date,
        future_schedule_action: FutureScheduleAction,
        reason: str | None = None,
        rehire_eligible: bool | None = None,
    ) -> dict[str, int | str]:
        """직원을 퇴사 처리한다.

        Args:
            termination_date: 퇴사일 (이 날짜까지는 근무한 것으로 본다)
            future_schedule_action: 퇴사일 **이후** 스케줄 처리 — unassign | delete
            reason: 퇴사 사유 (자유 텍스트)
            rehire_eligible: 재고용 가능 여부 (None = 미판단)

        Returns:
            처리 요약 — 영향받은 미래 스케줄 수 등

        Raises:
            EMPLOYEE_NOT_FOUND / MEMBERSHIP_NOT_FOUND: 사용자 또는 org 소속이 없을 때
            OFFBOARD_PROVISIONAL: 미가입(유령) 계정을 퇴사 처리하려 할 때
        """
        from app.services.user_service import user_service

        user: User | None = await db.scalar(
            select(User).where(
                User.id == user_id, User.organization_id == organization_id
            )
        )
        if user is None:
            raise EMPLOYEE_NOT_FOUND()
        if user.is_provisional:
            # 유령은 '아직 오지 않은 사람'이라 퇴사 개념이 없다. 제거는 delete 경로.
            raise OFFBOARD_PROVISIONAL()

        # lockout 방지 — 이 사람을 내보내면 조직을 관리할 사람이 아무도 남지 않는 경우 차단.
        # 이 상태가 되면 아무도 되돌릴 수 없어 DB 를 직접 만져야 복구된다 (§23.2).
        await self._guard_last_admin(db, user, organization_id)

        try:
            # 1) 재직 상태 — org 소속(OrgMember)이 진실의 원천
            member: OrgMember | None = await db.scalar(
                select(OrgMember).where(
                    OrgMember.user_id == user_id,
                    OrgMember.organization_id == organization_id,
                )
            )
            if member is None:
                raise MEMBERSHIP_NOT_FOUND()
            member.status = "terminated"
            member.termination_date = termination_date
            member.termination_reason = reason
            member.rehire_eligible = rehire_eligible

            # 2) 계정 비활성 + 자격증명 회수(PIN/email_verified/status 미러)
            user.is_active = False
            await db.flush()
            await user_service._apply_deactivation_side_effects(
                db, [user_id], organization_id
            )

            # 3) 미래 스케줄 — 퇴사일 이후만. 지난 근무 기록은 절대 건드리지 않는다.
            affected = await self._handle_future_schedules(
                db,
                user_id=user_id,
                organization_id=organization_id,
                after=termination_date,
                action=future_schedule_action,
            )

            await db.commit()
            return {
                "termination_date": termination_date.isoformat(),
                "future_schedules_affected": affected,
                "future_schedule_action": future_schedule_action,
            }
        except Exception:
            await db.rollback()
            raise

    async def _guard_last_admin(
        self, db: AsyncSession, user: User, organization_id: UUID
    ) -> None:
        """조직 최고 권한을 가진 마지막 한 사람이면 퇴사 처리를 막는다.

        판정은 "현재 활성자 중 가장 높은 권한(priority 최솟값)" 기준이다. 절대 등급이 아니라
        **지금 남아 있는 사람들 중 최상위**로 보는 이유는, 커스텀 role(D13/D19)에서
        고정된 owner 등급을 전제할 수 없기 때문이다.
        """
        top_priority = await db.scalar(
            select(func.min(Role.priority))
            .select_from(User)
            .join(Role, Role.id == User.role_id)
            .where(
                User.organization_id == organization_id,
                User.is_active.is_(True),
                User.is_provisional.is_(False),
            )
        )
        if top_priority is None:
            return
        target_priority = await db.scalar(
            select(Role.priority).where(Role.id == user.role_id)
        )
        if target_priority != top_priority:
            return
        holders = await db.scalar(
            select(func.count())
            .select_from(User)
            .join(Role, Role.id == User.role_id)
            .where(
                User.organization_id == organization_id,
                User.is_active.is_(True),
                User.is_provisional.is_(False),
                Role.priority == top_priority,
            )
        )
        if (holders or 0) <= 1:
            raise LAST_ADMIN()

    async def _handle_future_schedules(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        organization_id: UUID,
        after: date,
        action: FutureScheduleAction,
    ) -> int:
        """퇴사일 이후 스케줄 처리. 반환값 = 영향받은 건수.

        - unassign: `user_id=NULL` 로 비운다. 시간대·포지션은 남으므로 그리드에 빈 칸이
          보이고 대체자를 그 자리에 배정할 수 있다.
        - delete: status='deleted' 소프트 삭제 (기존 삭제 규약과 동일).
        """
        target = (
            Schedule.organization_id == organization_id,
            Schedule.user_id == user_id,
            Schedule.operating_day > after,
            Schedule.status.in_(["confirmed", "requested"]),
        )
        values = (
            {"user_id": None} if action == "unassign" else {"status": "deleted"}
        )
        result = await db.execute(sa_update(Schedule).where(*target).values(**values))
        await db.flush()
        return int(result.rowcount or 0)


offboarding_service = OffboardingService()
