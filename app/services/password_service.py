"""비밀번호 관리 서비스 — 아이디 찾기, 비밀번호 재설정, 비밀번호 변경, 관리자 초기화.

Password Management Service — Find username, reset password, change password,
and admin password reset business logic.
"""

import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.repositories.auth_repository import auth_repository
from app.services.email_verification_service import email_verification_service
from app.utils.email import send_email
from app.utils.email_templates import (
    build_password_reset_code_email,
    build_temporary_password_email,
)
from app.utils.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.utils.password import hash_password, verify_password


# 혼동 문자 제외 — Exclude confusing characters (0/O, 1/l/I)
_SAFE_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_SAFE_LOWER = "abcdefghjkmnpqrstuvwxyz"
_SAFE_DIGITS = "23456789"
_SAFE_ALPHABET = _SAFE_UPPER + _SAFE_LOWER + _SAFE_DIGITS


class PasswordService:
    """비밀번호 관리 비즈니스 로직."""

    def mask_username(self, username: str) -> str:
        """Username 마스킹.

        Rules:
            1-2 chars: mask last 1 (a→*, ab→a*)
            3-4 chars: mask last 2 (abc→a**, abcd→ab**)
            5+ chars: show first 2 + last 2, mask middle (john0123→jo****23)
        """
        n = len(username)
        if n <= 2:
            return username[:-1] + "*" if n == 2 else "*"
        if n <= 4:
            show = n - 2
            return username[:show] + "*" * (n - show)
        return username[:2] + "*" * (n - 4) + username[-2:]

    async def _get_user_by_email(self, db: AsyncSession, email: str) -> User:
        """이메일로 active 사용자 조회. 없으면 NotFoundError."""
        email = email.strip().lower()
        result = await db.execute(
            select(User).where(
                User.email == email,
                User.is_active == True,
            )
        )
        user = result.scalars().first()
        if user is None:
            raise NotFoundError("No account found with this email")
        return user

    # ── A. Find Username ──

    async def find_username_by_email(self, db: AsyncSession, email: str) -> str:
        """이메일로 마스킹된 username 반환."""
        user = await self._get_user_by_email(db, email)
        return self.mask_username(user.username)

    async def send_find_username_code(self, db: AsyncSession, email: str) -> dict:
        """아이디 찾기 인증코드 발송."""
        # 사용자 존재 확인
        await self._get_user_by_email(db, email)
        return await email_verification_service.send_code(
            db, email, purpose="find_username"
        )

    async def verify_find_username_code(
        self, db: AsyncSession, email: str, code: str
    ) -> str:
        """인증코드 검증 → full username 반환."""
        await email_verification_service.verify_code(db, email, code)
        user = await self._get_user_by_email(db, email)
        return user.username

    # ── B. Reset Password ──

    async def send_reset_password_code(
        self, db: AsyncSession, username: str, email: str
    ) -> dict:
        """비밀번호 재설정 인증코드 발송."""
        email = email.strip().lower()
        # username + email 조합 일치 확인
        result = await db.execute(
            select(User).where(
                User.username == username,
                User.email == email,
                User.is_active == True,
            )
        )
        if result.scalars().first() is None:
            raise NotFoundError("No account found")

        return await email_verification_service.send_code(
            db, email, purpose="reset_password"
        )

    async def verify_reset_password_code(
        self, db: AsyncSession, email: str, code: str
    ) -> str:
        """인증코드 검증 → reset_token (verification_token) 반환."""
        result = await email_verification_service.verify_code(db, email, code)
        return result["verification_token"]

    async def confirm_reset_password(
        self, db: AsyncSession, reset_token: str, new_password: str
    ) -> None:
        """reset_token 검증 → 비밀번호 변경 + 전 기기 로그아웃."""
        import uuid as uuid_mod
        from datetime import timedelta
        from app.models.email_verification import EmailVerificationCode

        try:
            token_uuid = uuid_mod.UUID(reset_token)
        except ValueError:
            raise BadRequestError("Invalid reset token")

        # reset_token으로 직접 레코드 조회 (10분 이내 생성된 것만)
        token_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        result = await db.execute(
            select(EmailVerificationCode).where(
                EmailVerificationCode.verification_token == token_uuid,
                EmailVerificationCode.is_used == True,
                EmailVerificationCode.created_at > token_cutoff,
            )
        )
        record = result.scalars().first()
        if record is None:
            raise BadRequestError("Invalid or expired reset token")

        # 해당 이메일의 사용자 찾기
        email = record.email
        user_result = await db.execute(
            select(User).where(
                User.email == email,
                User.is_active == True,
            )
        )
        user = user_result.scalars().first()
        if user is None:
            raise NotFoundError("User not found")

        # 비밀번호 변경
        user.password_hash = hash_password(new_password)
        user.password_changed_at = datetime.now(timezone.utc)
        user.must_change_password = False

        # 전체 refresh_tokens 삭제
        await auth_repository.delete_user_refresh_tokens(db, user.id)
        await db.commit()

    # ── C. Change Password (My Page) ──

    async def change_password(
        self,
        db: AsyncSession,
        user: User,
        current_password: str,
        new_password: str,
        client_type: str = "unknown",
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> dict:
        """현재 비밀번호 확인 → 변경 → 다른 세션 삭제 → 새 토큰 발급."""
        if not verify_password(current_password, user.password_hash):
            raise BadRequestError("Current password is incorrect")

        user.password_hash = hash_password(new_password)
        user.password_changed_at = datetime.now(timezone.utc)
        user.must_change_password = False

        # 전체 refresh_tokens 삭제
        await auth_repository.delete_user_refresh_tokens(db, user.id)

        # 현재 세션용 새 토큰 발급
        from app.services.auth_service import auth_service
        from sqlalchemy.orm import selectinload

        # role을 다시 로드
        result = await db.execute(
            select(User).options(selectinload(User.role)).where(User.id == user.id)
        )
        loaded_user = result.scalar_one()

        tokens = await auth_service._generate_tokens(
            db, loaded_user, loaded_user.role,
            client_type=client_type,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        await db.commit()

        return {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "message": "Password changed successfully",
        }

    # ── D. Admin Reset Password ──

    def _generate_temp_password(self) -> str:
        """8자리 임시 비밀번호 생성. 혼동 문자 제외."""
        while True:
            pw = "".join(secrets.choice(_SAFE_ALPHABET) for _ in range(8))
            if (
                any(c in _SAFE_UPPER for c in pw)
                and any(c in _SAFE_LOWER for c in pw)
                and any(c in _SAFE_DIGITS for c in pw)
            ):
                return pw

    async def admin_reset_password(
        self,
        db: AsyncSession,
        admin_user: User,
        target_user_id: str,
    ) -> str:
        """관리자 비밀번호 초기화 → 임시 비밀번호 생성 + 이메일 발송."""
        from uuid import UUID
        from sqlalchemy.orm import selectinload
        from app.repositories.user_repository import user_repository

        # role을 eager load하여 lazy loading 방지
        result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(
                User.id == UUID(target_user_id),
                User.organization_id == admin_user.organization_id,
            )
        )
        target = result.scalar_one_or_none()
        if target is None:
            raise NotFoundError("User not found")

        # 권한 검증: Owner(10) 전체, GM(20) 자기 매장 SV/Staff만
        admin_priority = admin_user.role.priority
        if admin_priority > 20:
            raise ForbiddenError("Insufficient permission")

        target_priority = target.role.priority
        if target_priority <= admin_priority:
            raise ForbiddenError("Cannot reset password for equal or higher role")

        if admin_priority == 20:
            admin_stores = await user_repository.get_managed_store_ids(db, admin_user.id)
            target_stores = await user_repository.get_work_store_ids(db, target.id)
            if not admin_stores or not target_stores:
                raise ForbiddenError("Target user not in your managed stores")
            if not set(target_stores) & set(admin_stores):
                raise ForbiddenError("Target user not in your managed stores")

        # 임시 비밀번호 생성 + 적용
        temp_password = self._generate_temp_password()
        target.password_hash = hash_password(temp_password)
        target.password_changed_at = datetime.now(timezone.utc)
        target.must_change_password = True

        # 전체 refresh_tokens 삭제
        await auth_repository.delete_user_refresh_tokens(db, target.id)

        # 이메일 발송
        if target.email:
            try:
                subject, html = build_temporary_password_email(temp_password)
                await send_email(to=target.email, subject=subject, html=html)
            except Exception:
                pass

        await db.commit()
        return temp_password


# 싱글턴 인스턴스 — Singleton instance
password_service: PasswordService = PasswordService()
