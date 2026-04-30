"""이메일 인증 서비스 — 인증코드 발송, 검증, 회원가입 토큰 검증.

Email Verification Service — Send verification codes, verify codes,
and validate verification tokens during registration.
"""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_verification import EmailVerificationCode
from app.models.user import User
from app.utils.email import send_email
from app.utils.email_templates import build_verification_code_email
from app.utils.exceptions import BadRequestError, ConflictError


# 인증코드 만료 시간 (5분)
CODE_EXPIRY_MINUTES = 5
# verification_token 만료 시간 (10분)
TOKEN_EXPIRY_MINUTES = 10
# 재발송 쿨다운 (60초)
RESEND_COOLDOWN_SECONDS = 60


class EmailVerificationService:
    """이메일 인증 비즈니스 로직."""

    async def send_code(
        self,
        db: AsyncSession,
        email: str,
        purpose: str = "registration",
    ) -> dict:
        """인증코드 발송.

        Args:
            db: DB session
            email: target email
            purpose: "registration" | "login_verify"

        Returns:
            dict with message and expires_in
        """
        email = email.strip().lower()

        # 이메일 중복 체크 — registration/login_verify에만 적용
        # find_username, reset_password는 기존 이메일이 있어야 하므로 skip
        if purpose in ("registration", "login_verify"):
            result = await db.execute(
                select(User).where(
                    User.email == email,
                    User.email_verified == True,
                )
            )
            if result.scalars().first() is not None:
                raise ConflictError("This email is already registered")

        # 재발송 쿨다운 체크
        cooldown_cutoff = datetime.now(timezone.utc) - timedelta(seconds=RESEND_COOLDOWN_SECONDS)
        result = await db.execute(
            select(EmailVerificationCode).where(
                EmailVerificationCode.email == email,
                EmailVerificationCode.purpose == purpose,
                EmailVerificationCode.created_at > cooldown_cutoff,
            )
        )
        if result.scalars().first() is not None:
            raise BadRequestError("Please wait before requesting another code")

        # 기존 미사용 코드 전부 무효화 (purpose 관계없이)
        await db.execute(
            update(EmailVerificationCode)
            .where(
                EmailVerificationCode.email == email,
                EmailVerificationCode.is_used == False,
            )
            .values(is_used=True)
        )

        # 6자리 코드 생성
        code = f"{secrets.randbelow(1000000):06d}"
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=CODE_EXPIRY_MINUTES)

        verification = EmailVerificationCode(
            email=email,
            code=code,
            purpose=purpose,
            expires_at=expires_at,
        )
        db.add(verification)
        await db.flush()

        # 이메일 발송 (SMTP 미설정 시 skip)
        try:
            subject, html = build_verification_code_email(code)
            await send_email(to=email, subject=subject, html=html)
        except Exception:
            # SMTP 미설정 등 발송 실패 시에도 코드는 DB에 저장됨
            pass

        await db.commit()

        return {"message": "Verification code sent", "expires_in": CODE_EXPIRY_MINUTES * 60}

    async def verify_code(
        self,
        db: AsyncSession,
        email: str,
        code: str,
    ) -> dict:
        """인증코드 검증. 성공 시 verification_token 발급.

        Returns:
            dict with verification_token and email
        """
        email = email.strip().lower()
        now = datetime.now(timezone.utc)

        # ── QA bypass — settings.EMAIL_VERIFICATION_TEST_CODE 가 설정되고 그 코드와 일치하면
        # 실제 DB record 없어도 verification_token 발급. worktree/local에서만 set, prod 미설정.
        from app.config import settings as _settings
        magic = (_settings.EMAIL_VERIFICATION_TEST_CODE or "").strip()
        if magic and code == magic:
            token = uuid.uuid4()
            # 미래 register/submit에서 validate_verification_token이 DB row를 찾으니
            # is_used=True 인 dummy row 한 개를 만들어 둠.
            dummy = EmailVerificationCode(
                email=email,
                code=magic,
                purpose="registration",
                expires_at=now + timedelta(minutes=10),
                is_used=True,
                verification_token=token,
            )
            db.add(dummy)
            await db.commit()
            return {"verification_token": str(token), "email": email}

        # 유효한 코드 조회
        result = await db.execute(
            select(EmailVerificationCode).where(
                EmailVerificationCode.email == email,
                EmailVerificationCode.is_used == False,
                EmailVerificationCode.expires_at > now,
            ).order_by(EmailVerificationCode.created_at.desc())
        )
        record = result.scalars().first()

        if record is None:
            raise BadRequestError("Invalid or expired verification code")

        # 시도 횟수 체크
        if record.attempts >= record.max_attempts:
            record.is_used = True
            await db.commit()
            raise BadRequestError("Too many attempts. Please request a new code")

        # 코드 일치 확인
        if record.code != code:
            record.attempts += 1
            remaining = record.max_attempts - record.attempts
            await db.commit()
            raise BadRequestError(f"Incorrect code. {remaining} attempts remaining")

        # 성공 — verification_token 발급
        token = uuid.uuid4()
        record.is_used = True
        record.verification_token = token
        await db.commit()

        return {
            "verification_token": str(token),
            "email": email,
        }

    async def validate_verification_token(
        self,
        db: AsyncSession,
        token_str: str,
        email: str,
    ) -> bool:
        """회원가입 시 verification_token 유효성 검증.

        Returns:
            True if valid
        """
        try:
            token_uuid = uuid.UUID(token_str)
        except ValueError:
            raise BadRequestError("Invalid verification token")

        now = datetime.now(timezone.utc)
        token_cutoff = now - timedelta(minutes=TOKEN_EXPIRY_MINUTES)

        result = await db.execute(
            select(EmailVerificationCode).where(
                EmailVerificationCode.verification_token == token_uuid,
                EmailVerificationCode.email == email.strip().lower(),
                EmailVerificationCode.is_used == True,
                EmailVerificationCode.created_at > token_cutoff,
            )
        )
        record = result.scalars().first()
        if record is None:
            raise BadRequestError("Invalid or expired verification token")

        return True

    async def confirm_email(
        self,
        db: AsyncSession,
        user: User,
        email: str,
        code: str,
    ) -> dict:
        """로그인 후 이메일 인증 (기존 사용자용).

        인증코드 검증 + users.email 업데이트 + email_verified=True 설정.
        """
        if user.email_verified:
            raise BadRequestError("Email is already verified")

        email = email.strip().lower()

        # 이메일 중복 체크 (다른 사용자가 이미 인증 완료한 이메일인지)
        result = await db.execute(
            select(User).where(
                User.email == email,
                User.email_verified == True,
                User.id != user.id,
            )
        )
        if result.scalars().first() is not None:
            raise ConflictError("This email is already used by another account")

        # 코드 검증
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(EmailVerificationCode).where(
                EmailVerificationCode.email == email,
                EmailVerificationCode.purpose == "login_verify",
                EmailVerificationCode.is_used == False,
                EmailVerificationCode.expires_at > now,
            ).order_by(EmailVerificationCode.created_at.desc())
        )
        record = result.scalars().first()

        if record is None:
            raise BadRequestError("Invalid or expired verification code")

        if record.attempts >= record.max_attempts:
            record.is_used = True
            await db.commit()
            raise BadRequestError("Too many attempts. Please request a new code")

        if record.code != code:
            record.attempts += 1
            remaining = record.max_attempts - record.attempts
            await db.commit()
            raise BadRequestError(f"Incorrect code. {remaining} attempts remaining")

        # 성공
        record.is_used = True
        user.email = email
        user.email_verified = True
        await db.commit()

        return {"message": "Email verified successfully"}


email_verification_service = EmailVerificationService()
