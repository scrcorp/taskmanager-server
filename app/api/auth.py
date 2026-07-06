"""공통 인증 라우터 — 토큰 갱신, 로그아웃, 프로필 조회, 비밀번호 관리.

Common Auth Router — Token refresh, logout, profile, password management endpoints.
Shared by both admin and app clients.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_account, get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    ChangePasswordResponse,
    ConsoleFiltersResponse,
    ConsoleFiltersUpdateRequest,
    FindUsernameRequest,
    SwitchOrgRequest,
    FindUsernameResponse,
    FindUsernameSendCodeRequest,
    FindUsernameVerifyCodeRequest,
    FindUsernameVerifyResponse,
    RefreshRequest,
    ResetPasswordConfirmRequest,
    ResetPasswordSendCodeRequest,
    ResetPasswordVerifyCodeRequest,
    ResetPasswordVerifyResponse,
    TokenResponse,
    UserMeResponse,
)
from app.services.auth_service import auth_service
from app.services.password_service import password_service

router: APIRouter = APIRouter()


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    data: RefreshRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    """토큰 갱신 — 리프레시 토큰으로 새 토큰 쌍 발급.

    Refresh token endpoint. Issues a new token pair using a refresh token.
    """
    from app.api.utils import get_session_info

    return await auth_service.refresh_tokens(
        db, data,
        **get_session_info(request),
    )


@router.post("/logout", status_code=204)
async def logout(
    data: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """로그아웃 — 리프레시 토큰 폐기.

    Logout endpoint. Revokes the given refresh token.
    """
    await auth_service.logout(db, data.refresh_token)


@router.get("/me", response_model=UserMeResponse)
async def get_me(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_account: Annotated[User, Depends(get_current_account)],
) -> UserMeResponse:
    """현재 사용자 프로필 조회.

    인증만 되면 200 — org 접근이 차단(라이센스/밴)돼도 소속 org 목록과 차단 이유(code)를
    함께 반환한다. 프론트가 이걸로 전용 화면/org 전환을 판단한다. (org-scoped 엔드포인트는
    get_current_user 로 게이트되어 403.)
    """
    return await auth_service.get_me(db, current_account)


@router.post("/switch-org", response_model=TokenResponse)
async def switch_org(
    data: SwitchOrgRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_account: Annotated[User, Depends(get_current_account)],
) -> TokenResponse:
    """소속된 다른 org 로 컨텍스트 전환 — 그 org 용 토큰 재발급 (접근 가능한 멤버여야).

    멀티-org 계정이 org 스위처로 전환할 때 사용. 대상 org 가 라이센스 정지/밴이면 403(코드).
    """
    from app.api.utils import get_session_info

    return await auth_service.switch_organization(
        db, current_account, data.organization_id, **get_session_info(request)
    )


@router.put("/me/console-filters", response_model=ConsoleFiltersResponse)
async def put_console_filters(
    data: ConsoleFiltersUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ConsoleFiltersResponse:
    """콘솔 페이지별 필터 영속 저장 (전체 교체).

    Replace the user's console_filters JSONB blob with the supplied value.
    Used by the console to keep filter/sort/page state in sync across devices.
    The client always sends the full object — last-write-wins, no merging.
    """
    # 빈 dict 거르기 — 큰 페이로드 방지: 키당 ~16개 param, 키 ~50개를 넘는 페이로드 거부.
    if len(data.filters) > 50:
        from fastapi import HTTPException
        raise HTTPException(status_code=413, detail="Too many filter pages")
    for v in data.filters.values():
        if len(v) > 32:
            from fastapi import HTTPException
            raise HTTPException(status_code=413, detail="Too many params per page")

    current_user.console_filters = data.filters
    await db.commit()
    await db.refresh(current_user, attribute_names=["console_filters"])
    return ConsoleFiltersResponse(console_filters=current_user.console_filters or {})


# ── Find Username ──


@router.post("/find-username", response_model=FindUsernameResponse)
async def find_username(
    data: FindUsernameRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FindUsernameResponse:
    """아이디 찾기 — 이메일로 마스킹된 username 조회."""
    masked = await password_service.find_username_by_email(db, data.email)
    return FindUsernameResponse(masked_username=masked)


@router.post("/find-username/send-code")
async def find_username_send_code(
    data: FindUsernameSendCodeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """아이디 찾기 인증코드 발송."""
    return await password_service.send_find_username_code(db, data.email)


@router.post("/find-username/verify-code", response_model=FindUsernameVerifyResponse)
async def find_username_verify_code(
    data: FindUsernameVerifyCodeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FindUsernameVerifyResponse:
    """아이디 찾기 인증코드 검증 → full username 반환."""
    username = await password_service.verify_find_username_code(db, data.email, data.code)
    return FindUsernameVerifyResponse(username=username)


# ── Reset Password ──


@router.post("/reset-password/send-code")
async def reset_password_send_code(
    data: ResetPasswordSendCodeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """비밀번호 재설정 인증코드 발송."""
    return await password_service.send_reset_password_code(db, data.username, data.email)


@router.post("/reset-password/verify-code", response_model=ResetPasswordVerifyResponse)
async def reset_password_verify_code(
    data: ResetPasswordVerifyCodeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ResetPasswordVerifyResponse:
    """비밀번호 재설정 인증코드 검증 → reset_token 반환."""
    token = await password_service.verify_reset_password_code(db, data.email, data.code)
    return ResetPasswordVerifyResponse(reset_token=token)


@router.post("/reset-password/confirm")
async def reset_password_confirm(
    data: ResetPasswordConfirmRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """비밀번호 재설정 확정 — reset_token + 새 비밀번호."""
    await password_service.confirm_reset_password(db, data.reset_token, data.new_password)
    return {"message": "Password reset successfully"}


# ── Change Password ──


@router.post("/change-password", response_model=ChangePasswordResponse)
async def change_password(
    data: ChangePasswordRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ChangePasswordResponse:
    """비밀번호 변경 — 현재 비밀번호 확인 후 변경, 새 토큰 발급."""
    from app.api.utils import get_session_info

    session_info = get_session_info(request)
    result = await password_service.change_password(
        db, current_user,
        data.current_password, data.new_password,
        client_type=session_info.get("client_type", "unknown"),
        user_agent=session_info.get("user_agent"),
        ip_address=session_info.get("ip_address"),
    )
    return ChangePasswordResponse(**result)
