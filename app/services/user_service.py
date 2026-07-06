"""사용자 서비스 — 사용자 CRUD 및 매장 배정 비즈니스 로직.

User Service — Business logic for user CRUD and store assignment operations.
Handles user management including creation, update, activation toggle,
and user-store association management.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization, Store
from app.models.user import Role, User
from app.repositories.store_repository import store_repository
from app.repositories.role_repository import role_repository
from app.repositories.user_repository import user_repository
from app.repositories.employee_no_history_repository import (
    employee_no_history_repository,
)
from app.schemas.organization import StoreResponse
from app.core.permissions import (
    OWNER_PRIORITY,
    STAFF_PRIORITY,
    SUPER_OWNER_PRIORITY,
    SV_PRIORITY,
    is_owner,
)
from app.schemas.user import (
    UserCreate,
    UserListResponse,
    UserResponse,
    UserStoreResponse,
    UserUpdate,
    _normalize_employee_no,
)
from app.utils.exceptions import BadRequestError, DuplicateError, ForbiddenError, NotFoundError
from app.utils.password import hash_password, verify_password


class UserService:
    """사용자 관련 비즈니스 로직을 처리하는 서비스.

    Service handling user business logic.
    Provides CRUD operations and store assignment management.
    """

    @staticmethod
    def _effective_rate(user_rate, org_rate) -> float | None:
        """effective hourly rate = user.hourly_rate (if set) → org default → None.

        DB 레벨에서는 상속 의미를 보존 (NULL은 '상속'). 응답 시점에만 계산.
        """
        if user_rate is not None:
            return float(user_rate)
        if org_rate is not None:
            return float(org_rate)
        return None

    def _to_response(
        self, user: User, org_rate: float | None = None, crewid: int | None = None
    ) -> UserResponse:
        """사용자 모델을 상세 응답 스키마로 변환 (effective rate 포함)."""
        role: Role = user.role
        raw_rate = float(user.hourly_rate) if user.hourly_rate is not None else None
        return UserResponse(
            id=str(user.id),
            username=user.username,
            full_name=user.full_name,
            email=user.email,
            email_verified=user.email_verified,
            role_name=role.name,
            role_priority=role.priority,
            hourly_rate=raw_rate,
            effective_hourly_rate=self._effective_rate(raw_rate, org_rate),
            department=user.department,
            employee_no=user.employee_no,
            crewid=crewid,
            is_active=user.is_active,
            created_at=user.created_at,
        )

    def _to_list_response(self, user: User, org_rate: float | None = None) -> UserListResponse:
        """사용자 모델을 목록 응답 스키마로 변환 (effective rate 포함)."""
        role: Role = user.role
        raw_rate = float(user.hourly_rate) if user.hourly_rate is not None else None
        return UserListResponse(
            id=str(user.id),
            username=user.username,
            full_name=user.full_name,
            email=user.email,
            email_verified=user.email_verified,
            role_name=role.name,
            role_priority=role.priority,
            hourly_rate=raw_rate,
            effective_hourly_rate=self._effective_rate(raw_rate, org_rate),
            department=user.department,
            employee_no=user.employee_no,
            is_active=user.is_active,
            created_at=user.created_at,
        )

    async def _get_org_rate(self, db: AsyncSession, organization_id: UUID) -> float | None:
        """조직 default_hourly_rate 한 번만 조회 (effective 계산용)."""
        r = await db.execute(
            select(Organization.default_hourly_rate).where(Organization.id == organization_id)
        )
        val = r.scalar()
        return float(val) if val is not None else None

    # 사번 영구 burn 메시지 — previously-used(과거 사용/현재 활성 모두 포함) 차단.
    _EMP_NO_BURNED_MSG = (
        "This employee number was previously used in this organization "
        "and cannot be reused."
    )

    async def _burn_check_and_record(
        self,
        db: AsyncSession,
        organization_id: UUID,
        employee_no: str | None,
        user_id: UUID | None,
    ) -> str | None:
        """사번 ledger 체크 + 기록 (옵션 A 영구 burn).

        Normalize → org 이력 조회 → 이미 존재하면 409(DuplicateError) →
        없으면 ledger 에 INSERT (first_assigned_user_id=user_id).
        호출자의 트랜잭션 안에서 동작하며 commit 하지 않는다 (원자성: 함께 롤백).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)
            employee_no: 부여할 사번 (validator 로 정규화되어 들어오지만 방어적으로 재정규화)
            user_id: 최초 부여 대상 유저 (Audit; ledger FK)

        Returns:
            str | None: 정규화된 사번 (None 이면 기록할 사번 없음)

        Raises:
            DuplicateError: 이력에 이미 존재(=burn)하는 사번일 때 (409)
        """
        normalized: str | None = _normalize_employee_no(employee_no)
        if normalized is None:
            return None
        burned: bool = await employee_no_history_repository.exists_for_org(
            db, organization_id, normalized
        )
        if burned:
            raise DuplicateError(self._EMP_NO_BURNED_MSG)
        try:
            await employee_no_history_repository.add(
                db, organization_id, normalized, user_id
            )
        except IntegrityError as e:
            # 동시 요청 TOCTOU: exists 체크 통과 후 같은 사번 동시 INSERT →
            # uq_emp_no_history_org_no 충돌. raw 500 대신 깔끔한 409 로 변환.
            raise DuplicateError(self._EMP_NO_BURNED_MSG) from e
        return normalized

    async def list_users(
        self,
        db: AsyncSession,
        organization_id: UUID,
        filters: dict[str, UUID | bool | None] | None = None,
    ) -> list[UserListResponse]:
        """조직에 속한 사용자 목록을 필터 조건으로 조회합니다.

        List users in the organization with optional filters.
        """
        users: list[User] = await user_repository.get_by_org(
            db, organization_id, filters
        )
        org_rate = await self._get_org_rate(db, organization_id)
        return [self._to_list_response(u, org_rate) for u in users]

    async def get_user(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
    ) -> UserResponse:
        """사용자 상세 정보를 조회합니다.

        Retrieve user detail with role information.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            UserResponse: 사용자 상세 응답 (User detail response)

        Raises:
            NotFoundError: 사용자를 찾을 수 없을 때 (User not found)
        """
        user: User | None = await user_repository.get_detail(
            db, user_id, organization_id
        )
        if user is None:
            raise NotFoundError("User not found")

        org_rate = await self._get_org_rate(db, organization_id)
        # CREWID — 이 org 에서의 org 번호 (org_member.crewid)
        from app.models.org_member import OrgMember

        crewid = (
            await db.execute(
                select(OrgMember.crewid).where(
                    OrgMember.user_id == user_id,
                    OrgMember.organization_id == organization_id,
                )
            )
        ).scalar_one_or_none()
        return self._to_response(user, org_rate, crewid=crewid)

    async def create_user(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: UserCreate,
        caller: User | None = None,
    ) -> UserResponse:
        """새 사용자를 생성합니다.

        Create a new user within an organization.
        Caller can only create users with role priority strictly greater than their own.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)
            data: 사용자 생성 데이터 (User creation data)
            caller: 요청자 (Caller user for level-based access control)

        Returns:
            UserResponse: 생성된 사용자 응답 (Created user response)

        Raises:
            DuplicateError: 같은 사용자명이 이미 존재할 때
                            (When the username already exists)
            NotFoundError: 지정한 역할을 찾을 수 없을 때 (Role not found)
            ForbiddenError: 자기보다 높거나 같은 우선순위의 역할 지정 시도
                            (Attempting to assign a role at or above caller's priority)
        """
        # 사용자명 중복 확인 — 전역 유니크 (Model B: 계정=전역, username 은 전역 로그인 아이디)
        exists: bool = await user_repository.exists(
            db, {"username": data.username}
        )
        if exists:
            raise DuplicateError("Username already exists")

        # 역할 유효성 확인 — Validate role exists in org
        role: Role | None = await role_repository.get_by_id(
            db, UUID(data.role_id), organization_id
        )
        if role is None:
            raise NotFoundError("Role not found")

        # 하위 직급만 생성 가능. Super Owner 는 동일 priority(또 다른 super_owner)도 허용 (다수 허용 단계).
        if caller is not None and caller.role:
            if role.priority < caller.role.priority:
                raise ForbiddenError("Cannot create a user with a role above your priority")
            if role.priority == caller.role.priority and caller.role.priority != SUPER_OWNER_PRIORITY:
                raise ForbiddenError("Cannot create a user with a role at your priority")

        password_hash: str = hash_password(data.password)

        # Auto-fill hourly_rate from org default if not provided
        hourly_rate = getattr(data, "hourly_rate", None)
        if hourly_rate is None:
            from app.models.organization import Organization as OrgModel
            org_row = await db.execute(
                select(OrgModel.default_hourly_rate).where(OrgModel.id == organization_id)
            )
            org_rate = org_row.scalar()
            hourly_rate = float(org_rate) if org_rate else None

        # Attendance device 용 clockin_pin 자동 발급
        from app.services.attendance_device_service import generate_clockin_pin

        clockin_pin = generate_clockin_pin()

        try:
            create_data: dict = {
                "organization_id": organization_id,
                "role_id": UUID(data.role_id),
                "username": data.username,
                "full_name": data.full_name,  # 스키마 validator 가 first/middle/last 로 합성 보장
                "email": data.email,
                "password_hash": password_hash,
                "clockin_pin": clockin_pin,
            }
            # 구조화된 이름 (first/middle/last) — 있으면 저장
            for _fld in ("first_name", "middle_name", "last_name"):
                _val = getattr(data, _fld, None)
                if _val is not None and _val.strip():
                    create_data[_fld] = _val.strip()
            if hourly_rate is not None:
                create_data["hourly_rate"] = hourly_rate
            # FOH/BOH 분류 — 지정된 경우만 저장 (미지정이면 NULL 유지)
            if data.department is not None:
                create_data["department"] = data.department

            # 사번 — 지정 시 org 이력(ledger) burn 체크 후 저장 (옵션 A 영구 burn).
            # FK 순서상 ledger 는 user 생성 후 기록(아래) — 여기선 burn 여부만 먼저 확인해
            # 활성/과거 중복 모두 깔끔한 409 로 반환 (partial unique IntegrityError 회피).
            normalized_emp: str | None = None
            if data.employee_no is not None:
                normalized_emp = _normalize_employee_no(data.employee_no)
                if normalized_emp is not None:
                    burned: bool = await employee_no_history_repository.exists_for_org(
                        db, organization_id, normalized_emp
                    )
                    if burned:
                        raise DuplicateError(self._EMP_NO_BURNED_MSG)
                    create_data["employee_no"] = normalized_emp

            user: User = await user_repository.create(
                db,
                create_data,
            )

            # 사번 ledger 기록 — user 생성 후(FK 충족). 같은 트랜잭션, 함께 커밋/롤백.
            if normalized_emp is not None:
                await employee_no_history_repository.add(
                    db, organization_id, normalized_emp, user.id
                )

            # [Model B] org 소속(org_member) 병행 생성 — 새 유저를 Model B 완결 엔티티로.
            # org별 속성(role/시급/부서/PIN/사번)을 org_member 에 미러(전환기: users 컬럼과 병존).
            from app.models.org_member import OrgMember
            from app.services.org_numbering import next_crewid

            _crewid = await next_crewid(db, organization_id)
            db.add(
                OrgMember(
                    user_id=user.id,
                    organization_id=organization_id,
                    role_id=UUID(data.role_id),
                    hourly_rate=hourly_rate,
                    department=create_data.get("department"),
                    clockin_pin=clockin_pin,
                    employee_no=normalized_emp,
                    status="active",
                    crewid=_crewid,
                )
            )
            await db.flush()

            # Owner / Super Owner 신규 생성 시 조직 내 모든 매장에 자동 배정
            # (is_manager=true, is_work_assignment=true — manager 면 work 자동). 알림 + 관리 권한 + 근무 배정 대상.
            if role.priority <= OWNER_PRIORITY:
                await user_repository.bulk_assign_org_stores_to_user(
                    db, user.id, organization_id
                )

            # 역할 관계 로드를 위해 다시 조회 — Re-fetch with role loaded
            loaded: User | None = await user_repository.get_detail(
                db, user.id, organization_id
            )
            if loaded is None:
                raise NotFoundError("User not found after creation")

            org_rate = await self._get_org_rate(db, organization_id)
            result = self._to_response(loaded, org_rate, crewid=_crewid)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def update_user(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
        data: UserUpdate,
        caller: User | None = None,
    ) -> UserResponse:
        """사용자 정보를 수정합니다.

        Update an existing user's information.
        When changing role_id, caller can only assign roles below their own priority.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            organization_id: 조직 ID (Organization UUID)
            data: 수정 데이터 (Update data)
            caller: 요청자 (Caller user for level-based access control)

        Returns:
            UserResponse: 수정된 사용자 응답 (Updated user response)

        Raises:
            NotFoundError: 사용자를 찾을 수 없을 때 (User not found)
            ForbiddenError: 자기보다 높거나 같은 우선순위의 역할 지정 시도
                            (Attempting to assign a role at or above caller's priority)
        """
        update_data: dict = data.model_dump(exclude_unset=True)

        # username 변경 시 조직 내 중복 검사 — Check username uniqueness within org
        if "username" in update_data and update_data["username"] is not None:
            new_username: str = update_data["username"].strip()
            if not new_username:
                raise BadRequestError("Username cannot be empty")
            update_data["username"] = new_username
            # 전역 유니크 체크 (username = 전역 로그인 아이디)
            exists: bool = await user_repository.exists(
                db, {"username": new_username}
            )
            if exists:
                # 자기 자신의 기존 username이면 무시
                current_user_obj: User | None = await user_repository.get_by_id(
                    db, user_id, organization_id
                )
                if current_user_obj is None or current_user_obj.username != new_username:
                    raise DuplicateError("Username already exists")

        # 이메일 변경 시 인증 상태 리셋 — Reset email_verified when email changes
        if "email" in update_data:
            current_user_obj_for_email: User | None = await user_repository.get_by_id(
                db, user_id, organization_id
            )
            if current_user_obj_for_email and update_data["email"] != current_user_obj_for_email.email:
                update_data["email_verified"] = False

        # 사번 변경 — 파일명·법적 문서(경고 PDF)에 발행시점 스냅샷되므로 보호.
        # '이미 부여된 사번'의 변경/삭제는 Owner 만 (신규 부여는 users:update 로 가능).
        # 실제 burn 체크+ledger 기록은 트랜잭션 내부에서 수행(아래 try 블록).
        record_emp_no: str | None = None  # 새로 burn+기록할 사번 (None=기록 안 함)
        if "employee_no" in update_data:
            new_emp: str | None = update_data["employee_no"]  # validator 로 normalize 완료
            emp_target: User | None = await user_repository.get_by_id(
                db, user_id, organization_id
            )
            if emp_target is None:
                raise NotFoundError("User not found")
            if emp_target.employee_no != new_emp:
                if emp_target.employee_no is not None and not (
                    caller is not None and is_owner(caller)
                ):
                    raise ForbiddenError(
                        "Only an Owner can change an existing employee number"
                    )
                # 새 non-null 값으로 변경될 때만 ledger burn+기록.
                # null 해제는 옛 번호 burn 을 그대로 유지(기록 안 함).
                if new_emp is not None:
                    record_emp_no = new_emp
            # 같은 값이면 no-op — 자기 현재 번호를 다시 기록하지 않는다.

        # role_id를 문자열에서 UUID로 변환 — Convert role_id from string to UUID
        if "role_id" in update_data and update_data["role_id"] is not None:
            role: Role | None = await role_repository.get_by_id(
                db, UUID(update_data["role_id"]), organization_id
            )
            if role is None:
                raise NotFoundError("Role not found")
            # 하위 직급만 지정 가능. Super Owner 는 동일 priority(또 다른 super_owner)도 허용 (다수 허용 단계).
            if caller is not None and caller.role:
                if role.priority < caller.role.priority:
                    raise ForbiddenError("Cannot assign a role above your priority")
                if role.priority == caller.role.priority and caller.role.priority != SUPER_OWNER_PRIORITY:
                    raise ForbiddenError("Cannot assign a role at your priority")
            update_data["role_id"] = UUID(update_data["role_id"])

            # 기존 role 조회 — Owner 승격/강등 판정용
            existing_user: User | None = await user_repository.get_detail(
                db, user_id, organization_id
            )
            prev_priority = existing_user.role.priority if existing_user and existing_user.role else None
            was_owner_tier = prev_priority is not None and prev_priority <= OWNER_PRIORITY
            is_owner_tier = role.priority <= OWNER_PRIORITY

            # Owner/Super Owner → 그 아래 role 강등: user_stores 전체 제거 (자동 배정의 역동작)
            if was_owner_tier and not is_owner_tier:
                await user_repository.remove_all_user_stores(db, user_id)
            # 그 아래 role → Owner/Super Owner 승격: 조직 내 모든 매장 자동 배정
            elif not was_owner_tier and is_owner_tier:
                await user_repository.bulk_assign_org_stores_to_user(
                    db, user_id, organization_id
                )
            # Staff 이하로 변경 시 (Owner 강등 케이스가 아니면) is_manager 만 초기화
            elif role.priority >= STAFF_PRIORITY:
                await user_repository.reset_manager_flags(db, user_id)

        try:
            # 사번 새 부여/변경 시 ledger burn 체크 + 기록.
            # user update 와 같은 트랜잭션 — burn 이면 DuplicateError(409) 후 함께 롤백.
            if record_emp_no is not None:
                await self._burn_check_and_record(
                    db, organization_id, record_emp_no, user_id
                )

            user: User | None = await user_repository.update(
                db, user_id, update_data, organization_id
            )
            if user is None:
                raise NotFoundError("User not found")

            # 역할 관계 로드를 위해 다시 조회 — Re-fetch with role loaded
            loaded: User | None = await user_repository.get_detail(
                db, user_id, organization_id
            )
            if loaded is None:
                raise NotFoundError("User not found")

            org_rate = await self._get_org_rate(db, organization_id)
            result = self._to_response(loaded, org_rate)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    # 일괄 변경 허용 컬럼 — role_id/store 는 가드/부수효과 때문에 제외 (후속 증분)
    BULK_ALLOWED_FIELDS = frozenset({"department", "is_active", "hourly_rate"})

    async def bulk_update_users(
        self,
        db: AsyncSession,
        organization_id: UUID,
        user_ids: list[str],
        changes: dict,
    ) -> int:
        """여러 직원의 필드를 일괄 변경합니다 (조직 스코프).

        Bulk-update the given fields for the given users.

        Args:
            changes: {필드: 값} — 보낸 필드만. 허용 필드만 적용.

        Returns:
            int: 실제 변경된 사용자 수 (rows updated)

        Raises:
            BadRequestError: 잘못된 UUID / 허용되지 않은 필드 / 변경 필드 없음
        """
        # 허용 필드만 통과 (그 외는 거부 — 조용히 무시하지 않고 명시 에러)
        unknown = set(changes) - self.BULK_ALLOWED_FIELDS
        if unknown:
            raise BadRequestError(f"Fields not allowed for bulk update: {', '.join(sorted(unknown))}")
        if not changes:
            raise BadRequestError("No fields to update")

        try:
            uuids = [UUID(uid) for uid in user_ids]
        except (ValueError, AttributeError):
            raise BadRequestError("Invalid user id in user_ids")

        try:
            count = await user_repository.bulk_update_fields(
                db, organization_id, uuids, changes
            )
            await db.commit()
            return count
        except Exception:
            await db.rollback()
            raise

    async def get_super_owner_status(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> dict:
        """조직의 Super Owner 발급 상태 조회.

        Returns:
            dict: { "exists": bool, "username": str|None }
        """
        from app.core.permissions import SUPER_OWNER_PRIORITY

        result = await db.execute(
            select(User.username)
            .join(Role, User.role_id == Role.id)
            .where(
                User.organization_id == organization_id,
                Role.priority == SUPER_OWNER_PRIORITY,
                User.deleted_at.is_(None),
            )
            .limit(1)
        )
        username = result.scalar_one_or_none()
        return {
            "exists": username is not None,
            "username": username,
        }

    async def transfer_super_owner(
        self,
        db: AsyncSession,
        caller: User,
        target_user_id: UUID,
        current_password: str,
    ) -> dict:
        """Super Owner 양도. caller(super_owner) → owner 강등, target(owner) → super_owner 승격.

        단일 트랜잭션. 매장 배정도 함께 처리:
        - caller(새 owner): 모든 매장 자동 배정
        - target(새 super_owner): 매장 user_stores 전체 제거

        Args:
            caller: 현재 super_owner (priority=5)
            target_user_id: 새 super_owner 가 될 사용자 (반드시 같은 조직의 Owner)
            current_password: 본인 확인용 caller 비밀번호

        Raises:
            ForbiddenError: caller 가 super_owner 가 아님 / 비밀번호 불일치
            NotFoundError: target 또는 owner role 미존재
            BadRequestError: target 이 같은 조직 Owner 가 아님 / 자기 자신
        """
        from app.core.permissions import OWNER_PRIORITY, SUPER_OWNER_PRIORITY

        # caller 가 super_owner 인지 검증
        if caller.role is None or caller.role.priority != SUPER_OWNER_PRIORITY:
            raise ForbiddenError("Only Super Owner can transfer ownership")

        # 자기 자신에게 양도 불가
        if caller.id == target_user_id:
            raise BadRequestError("Cannot transfer to yourself")

        # current_password 검증
        if not verify_password(current_password, caller.password_hash):
            raise ForbiddenError("Current password is incorrect")

        # target 조회 (같은 조직, 활성, 미삭제)
        target: User | None = await user_repository.get_detail(
            db, target_user_id, caller.organization_id
        )
        if target is None or not target.is_active or target.deleted_at is not None:
            raise NotFoundError("Target user not found")

        # target 이 Owner 인지 확인 (Super Owner 는 Owner 에게만 양도 가능)
        if target.role is None or target.role.priority != OWNER_PRIORITY:
            raise BadRequestError(
                "Target must be an Owner. Promote them to Owner first, then transfer."
            )

        # owner role / super_owner role 조회
        owner_role_q = await db.execute(
            select(Role).where(
                Role.organization_id == caller.organization_id,
                Role.priority == OWNER_PRIORITY,
            )
        )
        owner_role = owner_role_q.scalar_one_or_none()
        if owner_role is None:
            raise NotFoundError("Owner role not provisioned")

        super_owner_role_q = await db.execute(
            select(Role).where(
                Role.organization_id == caller.organization_id,
                Role.priority == SUPER_OWNER_PRIORITY,
            )
        )
        super_owner_role = super_owner_role_q.scalar_one_or_none()
        if super_owner_role is None:
            raise NotFoundError("Super Owner role not provisioned")

        try:
            # 1) target → super_owner role 로 변경
            await user_repository.update(
                db, target.id, {"role_id": super_owner_role.id}, caller.organization_id
            )
            # 2) target 의 매장 배정 전체 제거 (Super Owner 는 매장 운영 비참여)
            await user_repository.remove_all_user_stores(db, target.id)

            # 3) caller → owner role 로 강등
            await user_repository.update(
                db, caller.id, {"role_id": owner_role.id}, caller.organization_id
            )
            # 4) caller 를 조직 내 모든 매장에 자동 배정 (Owner 자동 배정 정책)
            await user_repository.bulk_assign_org_stores_to_user(
                db, caller.id, caller.organization_id
            )

            await db.commit()

            import logging
            logger = logging.getLogger("uvicorn.error")
            logger.info(
                "[super_owner_transfer] org=%s from=%s to=%s",
                caller.organization_id, caller.id, target.id,
            )

            return {
                "message": (
                    f"Super Owner transferred to {target.username}. "
                    "You are now Owner and assigned to all stores."
                ),
                "new_super_owner_user_id": str(target.id),
                "new_super_owner_username": target.username,
            }
        except Exception:
            await db.rollback()
            raise

    async def toggle_active(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
    ) -> UserResponse:
        """사용자 활성/비활성 상태를 토글합니다.

        Toggle a user's active/inactive status.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            UserResponse: 변경된 사용자 응답 (Updated user response)

        Raises:
            NotFoundError: 사용자를 찾을 수 없을 때 (User not found)
        """
        user: User | None = await user_repository.get_detail(
            db, user_id, organization_id
        )
        if user is None:
            raise NotFoundError("User not found")

        try:
            # 현재 상태 반전 — Invert current status
            toggled: User | None = await user_repository.update(
                db, user_id, {"is_active": not user.is_active}, organization_id
            )
            if toggled is None:
                raise NotFoundError("User not found")

            # 역할 관계 로드를 위해 다시 조회 — Re-fetch with role loaded
            loaded: User | None = await user_repository.get_detail(
                db, user_id, organization_id
            )
            if loaded is None:
                raise NotFoundError("User not found")

            org_rate = await self._get_org_rate(db, organization_id)
            result = self._to_response(loaded, org_rate)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def delete_user(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
    ) -> None:
        """사용자를 삭제합니다 (소프트 삭제: 비활성화).

        Delete a user (soft-delete: deactivate).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            organization_id: 조직 ID (Organization UUID)

        Raises:
            NotFoundError: 사용자를 찾을 수 없을 때 (User not found)
        """
        user: User | None = await user_repository.get_by_id(
            db, user_id, organization_id
        )
        if user is None:
            raise NotFoundError("User not found")

        try:
            # 소프트 삭제: 비활성화 — Soft-delete: deactivate user
            await user_repository.update(
                db, user_id, {"is_active": False}, organization_id
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def get_user_stores(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
    ) -> list[StoreResponse]:
        """사용자에게 배정된 매장 목록을 조회합니다.

        Retrieve all stores assigned to a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            organization_id: 조직 ID (Organization UUID)

        Returns:
            list[StoreResponse]: 배정된 매장 목록 (List of assigned store responses)

        Raises:
            NotFoundError: 사용자를 찾을 수 없을 때 (User not found)
        """
        # 사용자 존재 확인 — Verify user exists in org
        user: User | None = await user_repository.get_by_id(
            db, user_id, organization_id
        )
        if user is None:
            raise NotFoundError("User not found")

        from app.models.user_store import UserStore
        from app.models.org_member import OrgMember, OrgMemberStore

        assignments: list[UserStore] = await user_repository.get_user_store_assignments(db, user_id)
        # store 정보가 필요하므로 store 조회
        stores: list[Store] = await user_repository.get_user_stores(db, user_id)
        store_map = {s.id: s for s in stores}
        # EMPID map (store_id -> empid) from org_member_stores
        empid_rows = (
            await db.execute(
                select(OrgMemberStore.store_id, OrgMemberStore.empid)
                .join(OrgMember, OrgMember.id == OrgMemberStore.org_member_id)
                .where(OrgMember.user_id == user_id)
            )
        ).all()
        empid_map = {sid: emp for sid, emp in empid_rows}

        return [
            UserStoreResponse(
                id=str(a.store_id),
                organization_id=str(store_map[a.store_id].organization_id) if a.store_id in store_map else "",
                name=store_map[a.store_id].name if a.store_id in store_map else "",
                address=store_map[a.store_id].address if a.store_id in store_map else None,
                is_active=store_map[a.store_id].is_active if a.store_id in store_map else False,
                is_manager=a.is_manager,
                is_work_assignment=a.is_work_assignment,
                created_at=store_map[a.store_id].created_at if a.store_id in store_map else a.created_at,
                empid=empid_map.get(a.store_id),
            )
            for a in assignments
            if a.store_id in store_map
        ]

    async def sync_user_stores(
        self,
        db: AsyncSession,
        user_id: UUID,
        organization_id: UUID,
        assignments: list[dict],
    ) -> None:
        """매장 배정 일괄 저장 (diff 기반).

        Args:
            assignments: [{"store_id": UUID, "is_manager": bool}, ...]

        Raises:
            NotFoundError: 사용자 또는 매장을 찾을 수 없을 때
            BadRequestError: Role별 규칙 위반 시
        """
        user_with_role: User | None = await user_repository.get_detail(
            db, user_id, organization_id
        )
        if user_with_role is None:
            raise NotFoundError("User not found")

        priority = user_with_role.role.priority

        # 룰: is_manager=true 이면 is_work_assignment 도 자동 true (work 해제 불가).
        # API 직접 호출도 안전하게 강제.
        for a in assignments:
            if a.get("is_manager"):
                a["is_work_assignment"] = True

        # Role별 검증
        manager_count = sum(1 for a in assignments if a["is_manager"])

        if priority >= STAFF_PRIORITY and manager_count > 0:
            raise BadRequestError("Staff cannot be assigned as manager")

        if priority == SV_PRIORITY and manager_count > 1:
            raise BadRequestError("Supervisor can only manage one store")

        # 매장 존재 확인
        org_stores = await store_repository.get_by_org(db, organization_id)
        org_store_ids = {s.id for s in org_stores}
        for a in assignments:
            if a["store_id"] not in org_store_ids:
                raise NotFoundError(f"Store not found: {a['store_id']}")

        try:
            await user_repository.sync_user_stores(db, user_id, assignments)
            # [Model B] org_member_stores 도 동기화(+empid 부여) — 표시/배정의 새 소스
            from app.services.org_numbering import reconcile_member_stores
            await reconcile_member_stores(db, user_id, assignments)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def add_user_store(
        self,
        db: AsyncSession,
        user_id: UUID,
        store_id: UUID,
        organization_id: UUID,
        caller: User | None = None,
    ) -> None:
        """사용자에게 매장을 배정합니다 (개별 API용, 하위호환).

        Staff는 근무매장만 가능 (is_manager=false).
        Supervisor는 관리매장 1개만.
        """
        user_with_role: User | None = await user_repository.get_detail(
            db, user_id, organization_id
        )
        if user_with_role is None:
            raise NotFoundError("User not found")

        store: Store | None = await store_repository.get_by_id(
            db, store_id, organization_id
        )
        if store is None:
            raise NotFoundError("Store not found")

        already_exists: bool = await user_repository.user_store_exists(
            db, user_id, store_id
        )
        if already_exists:
            raise DuplicateError("User is already assigned to this store")

        try:
            await user_repository.add_user_store(db, user_id, store_id)
            from app.services.org_numbering import ensure_member_store
            await ensure_member_store(db, user_id, store_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def remove_user_store(
        self,
        db: AsyncSession,
        user_id: UUID,
        store_id: UUID,
        organization_id: UUID,
    ) -> None:
        """사용자에게서 매장 배정을 해제합니다.

        Remove a store assignment from a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 ID (User UUID)
            store_id: 매장 ID (Store UUID)
            organization_id: 조직 ID (Organization UUID)

        Raises:
            NotFoundError: 배정 관계를 찾을 수 없을 때 (Assignment not found)
        """
        try:
            removed: bool = await user_repository.remove_user_store(db, user_id, store_id)
            if not removed:
                raise NotFoundError("User-store assignment not found")
            from app.services.org_numbering import remove_member_store
            await remove_member_store(db, user_id, store_id)
            await db.commit()
        except Exception:
            await db.rollback()
            raise


# 싱글턴 인스턴스 — Singleton instance
user_service: UserService = UserService()
