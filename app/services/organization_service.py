"""조직 서비스 — 조직 조회 및 수정 비즈니스 로직.

Organization Service — Business logic for organization retrieval and update.
Provides current-organization scoped operations.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.repositories.organization_repository import organization_repository
from app.schemas.organization import OrganizationResponse, OrganizationUpdate
from app.utils.exceptions import NotFoundError


class OrganizationService:
    """조직 관련 비즈니스 로직을 처리하는 서비스.

    Service handling organization business logic.
    Provides read and update operations scoped to the current organization.
    """

    async def get_current(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> OrganizationResponse:
        """현재 조직 정보를 조회합니다.

        Retrieve the current organization's details.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID from JWT)

        Returns:
            OrganizationResponse: 조직 응답 (Organization response)

        Raises:
            NotFoundError: 조직을 찾을 수 없을 때 (Organization not found)
        """
        org: Organization | None = await organization_repository.get_by_id(
            db, organization_id
        )
        if org is None:
            raise NotFoundError("Organization not found")

        return OrganizationResponse(
            id=str(org.id),
            name=org.name,
            code=org.code,
            timezone=org.timezone,
            day_start_time=org.day_start_time.strftime("%H:%M") if org.day_start_time else None,
            weekly_overtime_limit=org.weekly_overtime_limit,
            default_hourly_rate=float(org.default_hourly_rate),

            is_active=org.is_active,
            created_at=org.created_at,
        )

    async def update_current(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: OrganizationUpdate,
    ) -> OrganizationResponse:
        """현재 조직 정보를 수정합니다.

        Update the current organization's details.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID from JWT)
            data: 수정할 데이터 (Update data)

        Returns:
            OrganizationResponse: 수정된 조직 응답 (Updated organization response)

        Raises:
            NotFoundError: 조직을 찾을 수 없을 때 (Organization not found)
        """
        try:
            update_data: dict[str, str | bool | None] = data.model_dump(exclude_unset=True)
            org: Organization | None = await organization_repository.update(
                db, organization_id, update_data
            )
            if org is None:
                raise NotFoundError("Organization not found")

            result = OrganizationResponse(
                id=str(org.id),
                name=org.name,
                code=org.code,
                timezone=org.timezone,
                day_start_time=org.day_start_time.strftime("%H:%M") if org.day_start_time else None,
                weekly_overtime_limit=org.weekly_overtime_limit,
                default_hourly_rate=float(org.default_hourly_rate),
    
                is_active=org.is_active,
                created_at=org.created_at,
            )
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def create_organization(
        self,
        db: AsyncSession,
        *,
        name: str,
        admin_username: str,
        admin_password: str,
        admin_email: str | None = None,
        timezone: str | None = None,
        first_store_name: str | None = None,
    ) -> dict:
        """새 조직을 부트스트랩 생성 (백오피스 운영자용).

        `/setup` 시퀀스를 재사용하되 org-count 게이트 없이 멀티-org 생성을 지원한다:
        org → 5 roles → role_permissions → 기본 템플릿(daily-report/eval/warning) →
        super_owner user(+org_member) → (옵션) 첫 store. 단일 트랜잭션.

        Returns: {org_id, code, name, admin_username, store_id}
        """
        from app.core.access_code import ensure_code
        from app.core.permissions import (
            SUPER_OWNER_PRIORITY, OWNER_PRIORITY, GM_PRIORITY, SV_PRIORITY, STAFF_PRIORITY,
        )
        from app.models.user import Role, User
        from app.models.organization import Store
        from app.models.org_member import OrgMember
        from app.models.license import License
        from app.models.permission import Permission, RolePermission
        from app.utils.password import hash_password
        from app.services.attendance_device_service import generate_clockin_pin
        from app.services.daily_report_service import daily_report_service
        from app.services.evaluation_service import evaluation_service
        from app.services.warning_category_service import warning_category_service

        try:
            # 1) org (code 자동 발급)
            org = Organization(name=name)
            if timezone:
                org.timezone = timezone
            db.add(org)
            await db.flush()

            # 1b) 라이센스 (active) — org 운영 자격
            db.add(License(organization_id=org.id, status="active", plan="trial"))

            # 1c) attendance 기기 등록 코드 (조직별, 자동 발급) — 태블릿 등록에 사용
            await ensure_code(db, "attendance", org.id)

            # 2) 기본 역할 5개
            super_owner_role: Role | None = None
            roles_created: list[Role] = []
            for rname, priority in [
                ("super_owner", SUPER_OWNER_PRIORITY),
                ("owner", OWNER_PRIORITY),
                ("general_manager", GM_PRIORITY),
                ("supervisor", SV_PRIORITY),
                ("staff", STAFF_PRIORITY),
            ]:
                role = Role(organization_id=org.id, name=rname, priority=priority)
                db.add(role)
                roles_created.append(role)
                if priority == SUPER_OWNER_PRIORITY:
                    super_owner_role = role
            await db.flush()
            assert super_owner_role is not None

            # 3) role_permissions (setup 과 동일 규칙)
            all_perms = {p.code: p.id for p in (await db.execute(select(Permission))).scalars().all()}
            gm_excluded = {"stores:create", "stores:delete", "roles:create", "roles:delete"}
            sv_allowed = {
                "stores:read", "users:read", "roles:read",
                "schedules:read", "schedules:create",
                "notices:read", "checklists:read", "tasks:read",
                "evaluations:read", "dashboard:read",
            }
            super_owner_only = {"org:delete", "owner:assign", "super_owner:transfer"}
            for r in roles_created:
                if r.priority <= SUPER_OWNER_PRIORITY:
                    codes = list(all_perms.keys())
                elif r.priority <= OWNER_PRIORITY:
                    codes = [c for c in all_perms if c not in super_owner_only]
                elif r.priority <= GM_PRIORITY:
                    codes = [c for c in all_perms if c not in gm_excluded and c not in super_owner_only]
                elif r.priority <= SV_PRIORITY:
                    codes = [c for c in all_perms if c in sv_allowed]
                else:
                    codes = []
                for code in codes:
                    db.add(RolePermission(role_id=r.id, permission_id=all_perms[code]))
            await db.flush()

            # 4) 기본 템플릿 (신규 org 즉시 보유)
            await daily_report_service.create_default_template_for_org(db, org.id)
            await evaluation_service.ensure_basic_template(db, org.id)
            await warning_category_service.seed_defaults(db, org.id)

            # 5) super_owner user + org_member
            clockin_pin = generate_clockin_pin()
            user = User(
                organization_id=org.id,
                role_id=super_owner_role.id,
                username=admin_username,
                full_name=admin_username,
                email=admin_email,
                password_hash=hash_password(admin_password),
                clockin_pin=clockin_pin,
            )
            db.add(user)
            await db.flush()
            db.add(OrgMember(
                user_id=user.id,
                organization_id=org.id,
                role_id=super_owner_role.id,
                clockin_pin=clockin_pin,
                status="active",
                crewid=1,  # 새 org 의 첫 멤버
            ))

            # 6) 첫 store (옵션)
            store = None
            if first_store_name:
                store = Store(organization_id=org.id, name=first_store_name, timezone=timezone)
                db.add(store)

            await db.flush()
            await db.refresh(org)
            result = {
                "org_id": org.id,
                "code": org.code,
                "name": org.name,
                "admin_username": admin_username,
                "store_id": store.id if store is not None else None,
            }
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise


# 싱글턴 인스턴스 — Singleton instance
organization_service: OrganizationService = OrganizationService()
