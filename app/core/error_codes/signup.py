"""가입 / 계정 점유 — 공개 링크(`/join`, `/direct`)로 들어오는 흐름.

콘솔의 가입 컴포넌트가 이 코드로 화면을 갈라 렌더하고(`InvalidLinkScreen`, `SignupFlow`),
staff 앱은 `provisional_candidate_exists` 로 "인수 코드" 안내를 띄운다.
전부 배포된 계약이라 개명 금지(X2).
"""

from __future__ import annotations

from app.core.error_codes._registry import domain

SIGNUP = domain("signup")

_CONSOLE_SIGNUP = (
    "console/src/types/signup.ts",
    "console/src/components/signup/SignupFlow.tsx",
    "console/src/components/signup/DirectSignupFlow.tsx",
)

INVALID_LINK = SIGNUP.legacy(
    "invalid_link",
    404,
    "This sign-up link is not valid.",
    hint="Ask your manager for a new link.",
    frozen=True,
    clients=("console/src/types/signup.ts", "console/src/components/signup/InvalidLinkScreen.tsx"),
)

SIGNUPS_PAUSED = SIGNUP.legacy(
    "signups_paused",
    404,
    "This store is not accepting new sign-ups right now.",
    frozen=True,
    clients=("console/src/types/signup.ts", "console/src/components/signup/InvalidLinkScreen.tsx"),
)

INVALID_CREDENTIALS = SIGNUP.legacy(
    "invalid_credentials",
    401,
    "That username or password is not correct.",
    frozen=True,
    clients=_CONSOLE_SIGNUP,
)

NOT_ELIGIBLE = SIGNUP.legacy(
    "not_eligible",
    403,
    "You are not eligible to apply to this store.",
    frozen=True,
    clients=_CONSOLE_SIGNUP,
)

USERNAME_TAKEN = SIGNUP.legacy(
    "username_taken",
    409,
    "This username is already in use.",
    frozen=True,
    clients=_CONSOLE_SIGNUP + ("console/src/hooks/useHiring.ts",),
)

# ⚠️ `applications.py` 는 이 두 코드를 `f"{field}_taken"` 으로 **문자열 조립**해서 만든다.
# 조립된 코드는 레지스트리 검사를 통과하지 못하므로(정적으로 보이지 않는다) 여기 등록해 둔다.
# 새 코드는 절대 조립하지 말 것 — 그 순간 G2/G4 보호 밖으로 나간다.
EMAIL_TAKEN = SIGNUP.legacy("email_taken", 409, "This email is already in use.")

CREDENTIAL_MISMATCH = SIGNUP.legacy(
    "credential_mismatch",
    409,
    "That username and password do not match an existing account.",
    frozen=True,
    clients=("console/src/components/signup/SignupFlow.tsx",),
)

CREDENTIALS_SPLIT = SIGNUP.legacy(
    "credentials_split",
    409,
    "Username and email belong to different existing accounts.",
)

ACTIVE_APPLICATION_EXISTS = SIGNUP.legacy(
    "active_application_exists",
    409,
    "You already have an application in progress at this store.",
    frozen=True,
    clients=("console/src/components/signup/SignupFlow.tsx",),
)

PROVISIONAL_CANDIDATE_EXISTS = SIGNUP.legacy(
    "provisional_candidate_exists",
    409,
    "An account may already be set up for you — ask your manager for the claim code, "
    "or continue to create a new account.",
    frozen=True,
    clients=(
        "app/apps/staff/lib/providers/auth_provider.dart",
        "app/apps/staff/lib/screens/auth/register_screen.dart",
    ),
)

PROVISIONAL_ACCOUNT = SIGNUP.legacy(
    "provisional_account",
    400,
    "This account has not been claimed yet.",
    hint="Share the claim code so the employee can sign up.",
)
