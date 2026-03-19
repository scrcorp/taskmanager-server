"""이메일 인증 관련 Pydantic 스키마.

Email verification request/response schemas.
"""

from pydantic import BaseModel


class SendVerificationCodeRequest(BaseModel):
    """인증코드 발송 요청."""
    email: str
    purpose: str = "registration"  # "registration" | "login_verify"


class VerifyEmailCodeRequest(BaseModel):
    """인증코드 검증 요청."""
    email: str
    code: str


class ConfirmEmailRequest(BaseModel):
    """로그인 후 이메일 인증 요청 (기존 사용자용)."""
    email: str
    code: str
