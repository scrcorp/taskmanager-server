"""인증 서비스 — 로그인, 회원가입, 토큰 갱신 비즈니스 로직.

Auth Service — Business logic for login, registration, and token refresh.
Handles admin/app login separation, JWT token lifecycle,
and user profile retrieval.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.organization import Organization
from app.models.user import Role, User
from app.repositories.auth_repository import auth_repository
from app.repositories.role_repository import role_repository
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserMeResponse,
)
from app.utils.exceptions import (
    BadRequestError,
    DuplicateError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)
from app.utils.jwt import create_access_token, create_refresh_token, decode_token
from app.utils.password import hash_password, verify_password


class AuthService:
    """인증 관련 비즈니스 로직을 처리하는 서비스.

    Service handling authentication business logic.
    Manages admin/app login flows, registration, token refresh, and logout.
    """

    async def resolve_company_code(
        self,
        db: AsyncSession,
        company_code: str | None,
    ) -> UUID | None:
        """회사 코드를 조직 UUID로 변환합니다.

        Resolve a company code to an organization UUID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            company_code: 회사 코드 (Company code, may be None)

        Returns:
            UUID | None: 조직 UUID 또는 None (Organization UUID or None)

        Raises:
            NotFoundError: 유효하지 않은 회사 코드일 때 (Invalid company code)
        """
        if company_code is None:
            return None
        result = await db.execute(
            select(Organization).where(
                Organization.code == company_code.upper(),
                Organization.is_active == True,
            )
        )
        org: Organization | None = result.scalar_one_or_none()
        if org is None:
            raise NotFoundError("Invalid company code")
        return org.id

    def _build_jwt_payload(self, user: User, role: Role) -> dict[str, str | int]:
        """JWT 토큰 페이로드를 생성합니다.

        Build the JWT token payload from user and role data.

        Args:
            user: 사용자 모델 (User model instance)
            role: 역할 모델 (Role model instance)

        Returns:
            dict[str, str | int]: JWT 페이로드 딕셔너리 (JWT payload dictionary)
        """
        return {
            "sub": str(user.id),
            "org": str(user.organization_id),
            "role": role.name,
            "priority": role.priority,
        }

    async def _generate_tokens(
        self,
        db: AsyncSession,
        user: User,
        role: Role,
    ) -> TokenResponse:
        """액세스 토큰과 리프레시 토큰을 생성합니다.

        Generate access and refresh token pair for a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user: 사용자 모델 (User model instance)
            role: 역할 모델 (Role model instance)

        Returns:
            TokenResponse: 토큰 응답 (Token response with access and refresh tokens)
        """
        payload: dict[str, str | int] = self._build_jwt_payload(user, role)
        access_token: str = create_access_token(payload)
        refresh_token: str = create_refresh_token(payload)

        # 기존 리프레시 토큰 정리 — Clean up old refresh tokens to prevent accumulation
        await auth_repository.delete_user_refresh_tokens(db, user.id)

        # 리프레시 토큰을 DB에 저장 — Persist refresh token to database
        expires_at: datetime = datetime.now(timezone.utc) + timedelta(
            days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS
        )
        await auth_repository.create_refresh_token(
            db, user_id=user.id, token=refresh_token, expires_at=expires_at
        )

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
        )

    async def admin_login(
        self,
        db: AsyncSession,
        data: LoginRequest,
        organization_id: UUID | None = None,
    ) -> TokenResponse:
        """관리자 로그인을 처리합니다.

        Process admin login. Rejects staff-level accounts (level >= 4).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            data: 로그인 요청 데이터 (Login request data)
            organization_id: 조직 ID 필터 (Organization ID filter)

        Returns:
            TokenResponse: 토큰 응답 (Token response)

        Raises:
            UnauthorizedError: 잘못된 인증 정보일 때 (Invalid credentials)
            ForbiddenError: 스태프 계정이 관리자 로그인을 시도할 때
                            (Staff account attempting admin login)
        """
        user: User | None = await auth_repository.get_user_by_username(
            db, data.username, organization_id
        )
        if user is None or not verify_password(data.password, user.password_hash):
            raise UnauthorizedError("Invalid username or password")

        if not user.is_active:
            raise UnauthorizedError("Account is deactivated")

        role: Role = user.role

        # permission이 없으면 관리자 로그인 불가
        from app.repositories.permission_repository import permission_repository
        user_permissions = await permission_repository.get_permissions_by_role_id(db, user.role_id)
        if len(user_permissions) == 0:
            raise ForbiddenError("No admin permissions assigned to this role")

        return await self._generate_tokens(db, user, role)

    async def app_login(
        self,
        db: AsyncSession,
        data: LoginRequest,
        organization_id: UUID | None = None,
    ) -> TokenResponse:
        """앱 로그인을 처리합니다.

        Process app login. Allows staff (level 4) and supervisor (level 3) accounts.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            data: 로그인 요청 데이터 (Login request data)
            organization_id: 조직 ID 필터 (Organization ID filter)

        Returns:
            TokenResponse: 토큰 응답 (Token response)

        Raises:
            UnauthorizedError: 잘못된 인증 정보일 때 (Invalid credentials)
        """
        user: User | None = await auth_repository.get_user_by_username(
            db, data.username, organization_id
        )
        if user is None or not verify_password(data.password, user.password_hash):
            raise UnauthorizedError("Invalid username or password")

        if not user.is_active:
            raise UnauthorizedError("Account is deactivated")

        role: Role = user.role
        return await self._generate_tokens(db, user, role)

    async def app_register(
        self,
        db: AsyncSession,
        data: RegisterRequest,
        organization_id: UUID,
    ) -> TokenResponse:
        """앱 사용자 회원가입을 처리합니다.

        Process app user registration. Creates a staff-level (level 4) user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            data: 회원가입 요청 데이터 (Registration request data)
            organization_id: 소속 조직 ID (Organization UUID)

        Returns:
            TokenResponse: 토큰 응답 (Token response)

        Raises:
            DuplicateError: 같은 사용자명이 이미 존재할 때
                            (When the username already exists)
            BadRequestError: 스태프 역할을 찾을 수 없을 때
                             (When staff role is not found)
        """
        # 조직 존재 여부 확인 — Validate organization exists and is active
        org_result = await db.execute(
            select(Organization).where(
                Organization.id == organization_id,
                Organization.is_active == True,
            )
        )
        if org_result.scalar_one_or_none() is None:
            raise NotFoundError("Organization not found or inactive")

        # 사용자명 중복 확인 — Check username uniqueness
        existing: User | None = await auth_repository.get_user_by_username(
            db, data.username, organization_id
        )
        if existing is not None:
            raise DuplicateError("Username already exists")

        # 스태프 역할 조회 (priority = 40)
        roles: list[Role] = await role_repository.get_by_org(db, organization_id)
        staff_role: Role | None = None
        for r in roles:
            if r.priority == 40:
                staff_role = r
                break

        if staff_role is None:
            raise BadRequestError("Staff role not configured for this organization")

        # 사용자 생성 — Create user
        password_hash: str = hash_password(data.password)
        user: User = User(
            organization_id=organization_id,
            role_id=staff_role.id,
            username=data.username,
            full_name=data.full_name,
            email=data.email,
            password_hash=password_hash,
        )
        db.add(user)
        await db.flush()
        await db.refresh(user)

        return await self._generate_tokens(db, user, staff_role)

    async def refresh_tokens(
        self,
        db: AsyncSession,
        data: RefreshRequest,
    ) -> TokenResponse:
        """리프레시 토큰으로 새 토큰 쌍을 발급합니다.

        Issue a new token pair using a refresh token.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            data: 리프레시 요청 데이터 (Refresh request data)

        Returns:
            TokenResponse: 새 토큰 응답 (New token response)

        Raises:
            UnauthorizedError: 유효하지 않거나 만료된 리프레시 토큰일 때
                               (Invalid or expired refresh token)
        """
        # DB에서 리프레시 토큰 확인 — Verify refresh token in database
        db_token = await auth_repository.get_refresh_token(db, data.refresh_token)
        if db_token is None:
            raise UnauthorizedError("Invalid refresh token")

        # 만료 확인 — Check expiration
        if db_token.expires_at < datetime.now(timezone.utc):
            await auth_repository.delete_refresh_token(db, data.refresh_token)
            raise UnauthorizedError("Refresh token has expired")

        # JWT 디코딩으로 사용자 정보 추출 — Extract user info from JWT
        try:
            payload: dict = decode_token(data.refresh_token)
        except Exception:
            await auth_repository.delete_refresh_token(db, data.refresh_token)
            raise UnauthorizedError("Invalid refresh token")

        user_id: str | None = payload.get("sub")
        if user_id is None:
            raise UnauthorizedError("Invalid refresh token payload")

        # 직접 ID로 사용자 및 역할 조회 — Look up user and role by ID
        result = await db.execute(
            select(User).options(selectinload(User.role)).where(User.id == UUID(user_id))
        )
        user: User | None = result.scalar_one_or_none()

        if user is None or not user.is_active:
            raise UnauthorizedError("User not found or inactive")

        # 기존 리프레시 토큰 삭제 후 새 토큰 발급 — Delete old token and issue new pair
        await auth_repository.delete_refresh_token(db, data.refresh_token)
        return await self._generate_tokens(db, user, user.role)

    async def logout(
        self,
        db: AsyncSession,
        refresh_token: str,
    ) -> None:
        """로그아웃 처리 — 리프레시 토큰을 삭제합니다.

        Process logout by deleting the refresh token.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            refresh_token: 삭제할 리프레시 토큰 (Refresh token to revoke)
        """
        await auth_repository.delete_refresh_token(db, refresh_token)

    async def get_me(
        self,
        db: AsyncSession,
        user: User,
    ) -> UserMeResponse:
        """현재 로그인한 사용자 프로필을 반환합니다.

        Return the profile of the currently authenticated user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user: 인증된 사용자 모델 (Authenticated user model)

        Returns:
            UserMeResponse: 사용자 프로필 응답 (User profile response)
        """
        # 역할 및 조직 정보 로드 — Load role and organization data
        result = await db.execute(
            select(User)
            .options(selectinload(User.role), selectinload(User.organization))
            .where(User.id == user.id)
        )
        loaded_user: User | None = result.scalar_one_or_none()
        if loaded_user is None:
            raise NotFoundError("User not found")

        role: Role = loaded_user.role
        org: Organization = loaded_user.organization

        # permission codes 조회
        from app.repositories.permission_repository import permission_repository
        permissions = await permission_repository.get_permissions_by_role_id(db, role.id)

        return UserMeResponse(
            id=str(loaded_user.id),
            username=loaded_user.username,
            full_name=loaded_user.full_name,
            email=loaded_user.email,
            role_name=role.name,
            role_priority=role.priority,
            organization_id=str(loaded_user.organization_id),
            organization_name=org.name,
            company_code=org.code,
            is_active=loaded_user.is_active,
            permissions=sorted(permissions),
        )


# 싱글턴 인스턴스 — Singleton instance
auth_service: AuthService = AuthService()
