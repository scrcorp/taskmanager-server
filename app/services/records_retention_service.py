"""인사 기록 보존 — purge 후보 조회와 익명화. (D4 · §6)

원칙 두 가지.

1. **자동 삭제하지 않는다.** 보존 기간이 지나면 "후보 목록"에만 뜨고, 실행은 관리자가
   명시적으로 한다. 조용히 사라지는 인사 기록은 감사에서 되돌릴 방법이 없다.
2. **삭제는 두 단계로 나뉜다.** 익명화(이름·연락처·서명 제거, 근무·급여 집계는 유지) →
   완전 삭제(행 제거). **v1 은 익명화까지만 구현한다** — 완전 삭제는 급여·세무 기록과
   얽혀 있어 별도 결정이 필요하다.

설계: docs/99_inbox/2026-08-13-조직계층-재정의.md §6 · D4
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes.employment import (
    EMPLOYEE_NOT_FOUND,
    MEMBERSHIP_NOT_FOUND,
    RETENTION_NOT_ELAPSED,
)
from app.models.org_member import OrgMember
from app.models.user import User
from app.utils.settings_resolver import SettingNotRegisteredError, resolve_setting

RETENTION_SETTING_KEY = "employment.record_retention_years"
DEFAULT_RETENTION_YEARS = 7


def _shift_years(value: date, years: int) -> date:
    """N년 뒤 같은 날짜. 2/29 는 2/28 로 내린다 (윤년 보정)."""
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


class RecordsRetentionService:
    """보존 기간 판정과 익명화."""

    async def retention_years(self, db: AsyncSession, organization_id: UUID) -> int:
        """보존 기간(년). 값이 없거나 이상하면 기본값으로 떨어진다.

        설정은 서버 기동 시 registry 에 동기화되므로, 배포 직후처럼 아직 등록 전인
        순간이 있을 수 있다. 그때 500 을 내는 대신 기본값을 쓴다 — 보존 기간을 못 읽었다고
        해서 인사 기록 조회가 막힐 이유는 없다.
        """
        try:
            raw = await resolve_setting(db, RETENTION_SETTING_KEY, organization_id)
        except SettingNotRegisteredError:
            return DEFAULT_RETENTION_YEARS
        try:
            years = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_RETENTION_YEARS
        return years if years > 0 else DEFAULT_RETENTION_YEARS

    async def purge_candidates(
        self,
        db: AsyncSession,
        organization_id: UUID,
        *,
        today: date,
    ) -> dict[str, object]:
        """보존 기간이 지난 퇴사자 목록. **여기서 아무것도 지우지 않는다.**

        이미 익명화된 사람은 목록에서 빠진다 (할 일이 없다).
        """
        years = await self.retention_years(db, organization_id)
        rows = (await db.execute(
            select(OrgMember, User)
            .join(User, User.id == OrgMember.user_id)
            .where(
                OrgMember.organization_id == organization_id,
                OrgMember.status == "terminated",
                OrgMember.termination_date.is_not(None),
            )
            .order_by(OrgMember.termination_date)
        )).all()

        candidates: list[dict[str, object]] = []
        for member, user in rows:
            eligible_on = _shift_years(member.termination_date, years)
            if eligible_on > today:
                continue
            if user.status == "anonymized":
                continue
            candidates.append({
                "user_id": str(user.id),
                "full_name": user.full_name,
                "crewid": member.crewid,
                "termination_date": member.termination_date.isoformat(),
                "eligible_on": eligible_on.isoformat(),
            })
        return {
            "retention_years": years,
            "as_of": today.isoformat(),
            "candidates": candidates,
        }

    async def anonymize(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
        *,
        today: date,
    ) -> dict[str, str]:
        """개인 식별 정보를 제거한다. 근무·급여 집계는 그대로 유지된다.

        보존 기간이 지난 퇴사자만 대상 — 재직자나 기간 미도래자는 거부한다.
        되돌릴 수 없으므로 이 가드가 유일한 방어선이다.
        """
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

        years = await self.retention_years(db, organization_id)
        if member.status != "terminated" or member.termination_date is None:
            raise RETENTION_NOT_ELAPSED()
        if _shift_years(member.termination_date, years) > today:
            raise RETENTION_NOT_ELAPSED()

        label = f"Former Employee #{member.crewid}" if member.crewid else "Former Employee"
        try:
            user.full_name = label
            user.first_name = None
            user.middle_name = None
            user.last_name = None
            user.email = None
            user.email_verified = False
            user.clockin_pin = None
            user.signature_image_key = None
            user.signature_strokes = None
            # username 은 전역 unique — 로그인 경로를 확실히 끊으면서 충돌하지 않게 치환
            user.username = f"anon_{user.id.hex[:12]}"
            user.is_active = False
            user.status = "anonymized"
            await db.commit()
            return {"user_id": str(user_id), "full_name": label}
        except Exception:
            await db.rollback()
            raise


records_retention_service = RecordsRetentionService()
