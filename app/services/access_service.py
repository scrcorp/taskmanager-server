"""접근 판정 서비스 — 사용자가 특정 org 에 접근 가능한지 + 그 이유(코드).

한 사람이 여러 org 에 소속될 수 있고(Model B), 접근이 막히는 이유가 여러 가지다:
  - ORG_LICENSE_INACTIVE : 그 org 의 라이센스가 정지/만료 (org 전체 먹통)
  - ORG_ACCESS_REVOKED   : 그 org 에서 본인 멤버십이 terminated (본인만 밴/퇴출)
  - NOT_A_MEMBER         : 그 org 소속이 아님 (토큰 org 위조/무효)
접근 가능하면 reason = None.

프론트가 텍스트가 아니라 이 코드로 분기하고, /me·로그인이 소속 org 목록 + 상태를 함께
내려줘 "다른 org 로 전환" 등을 판단하게 한다.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.license import License
from app.models.org_member import OrgMember
from app.models.organization import Organization
from app.models.user import Role, User

# 접근 차단 이유 코드 (프론트 계약)
REASON_LICENSE_INACTIVE = "ORG_LICENSE_INACTIVE"
REASON_ACCESS_REVOKED = "ORG_ACCESS_REVOKED"
REASON_NOT_A_MEMBER = "NOT_A_MEMBER"


def _license_blocked(lic_status: str | None, expires_at, now: datetime) -> bool:
    """라이센스가 접근 차단 상태인지 (없으면 차단 아님 = fail-open)."""
    if lic_status is None:
        return False
    if lic_status != "active":
        return True
    return expires_at is not None and expires_at < now


class AccessService:
    async def block_reason_for_org(
        self, db: AsyncSession, user: User, org_id: UUID
    ) -> tuple[str | None, dict]:
        """(reason_code_or_None, {organization_name, organization_code}) 반환.

        reason=None 이면 접근 가능. member 가 없고 home org(legacy)도 아니면 NOT_A_MEMBER.
        """
        member = (
            await db.execute(
                select(OrgMember.status).where(
                    OrgMember.user_id == user.id,
                    OrgMember.organization_id == org_id,
                )
            )
        ).scalar_one_or_none()

        row = (
            await db.execute(
                select(Organization.name, Organization.code, License.status, License.expires_at)
                .select_from(Organization)
                .outerjoin(License, License.organization_id == Organization.id)
                .where(Organization.id == org_id)
            )
        ).first()
        info = {
            "organization_name": row[0] if row else None,
            "organization_code": row[1] if row else None,
        }
        lic_status = row[2] if row else None
        lic_expires = row[3] if row else None
        now = datetime.now(timezone.utc)

        # 1) 멤버십: 없으면 legacy home org 만 통과, 아니면 NOT_A_MEMBER
        if member is None:
            if user.organization_id != org_id:
                return REASON_NOT_A_MEMBER, info
        elif member == "terminated":
            return REASON_ACCESS_REVOKED, info

        # 2) 라이센스
        if _license_blocked(lic_status, lic_expires, now):
            return REASON_LICENSE_INACTIVE, info

        return None, info

    async def list_user_orgs(self, db: AsyncSession, user: User) -> list[dict]:
        """user 가 소속된 모든 org + 각 상태 (org 스위처/차단화면용).

        각 항목: organization_id, organization_name, organization_code, role_name,
                 role_priority, member_status, license_status, accessible, block_reason.
        legacy(멤버십 없는 home org)도 fallback 으로 포함해 스위처가 항상 현재 org 를 보이게 함.
        """
        now = datetime.now(timezone.utc)
        rows = (
            await db.execute(
                select(
                    OrgMember.organization_id,
                    Organization.name,
                    Organization.code,
                    Role.name,
                    Role.priority,
                    OrgMember.status,
                    License.status,
                    License.expires_at,
                )
                .join(Organization, Organization.id == OrgMember.organization_id)
                .join(Role, Role.id == OrgMember.role_id)
                .outerjoin(License, License.organization_id == OrgMember.organization_id)
                .where(OrgMember.user_id == user.id)
                .order_by(Organization.name)
            )
        ).all()

        result: list[dict] = []
        seen: set = set()
        for org_id, oname, ocode, rname, rprio, mstatus, lstatus, lexp in rows:
            reason = None
            if mstatus == "terminated":
                reason = REASON_ACCESS_REVOKED
            elif _license_blocked(lstatus, lexp, now):
                reason = REASON_LICENSE_INACTIVE
            result.append({
                "organization_id": str(org_id),
                "organization_name": oname,
                "organization_code": ocode,
                "role_name": rname,
                "role_priority": rprio,
                "member_status": mstatus,
                "license_status": lstatus,
                "accessible": reason is None,
                "block_reason": reason,
            })
            seen.add(org_id)

        # legacy fallback — 멤버십 없는 home org 를 목록에 포함
        if user.organization_id not in seen:
            row = (
                await db.execute(
                    select(Organization.name, Organization.code, License.status, License.expires_at)
                    .select_from(Organization)
                    .outerjoin(License, License.organization_id == Organization.id)
                    .where(Organization.id == user.organization_id)
                )
            ).first()
            if row is not None:
                lstatus, lexp = row[2], row[3]
                reason = REASON_LICENSE_INACTIVE if _license_blocked(lstatus, lexp, now) else None
                result.append({
                    "organization_id": str(user.organization_id),
                    "organization_name": row[0],
                    "organization_code": row[1],
                    "role_name": user.role.name if user.role else None,
                    "role_priority": user.role.priority if user.role else None,
                    "member_status": "active",
                    "license_status": lstatus,
                    "accessible": reason is None,
                    "block_reason": reason,
                })
        return result


access_service = AccessService()
