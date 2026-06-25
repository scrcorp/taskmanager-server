"""Backoffice 라우트 — 로그인/로그아웃/대시보드 셸.

All routes are include_in_schema=False (OpenAPI/docs 비노출). Mounted under the
secret slug prefix in main.py. Auth is the signed session cookie only — fully
independent of the org RBAC.
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.backoffice import pages, ratelimit
from app.api.backoffice import session as cp_session
from app.api.backoffice.deps import COOKIE_NAME, get_current_admin
from app.config import settings
from app.utils.password import verify_password

router: APIRouter = APIRouter(include_in_schema=False)


def _base() -> str:
    """마운트된 비밀경로 prefix (예: /_cp_xxx)."""
    return "/" + settings.BACKOFFICE_PATH.strip("/")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/")
async def root(request: Request) -> RedirectResponse:
    base = _base()
    target = "/dashboard" if get_current_admin(request) else "/login"
    return RedirectResponse(f"{base}{target}", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    base = _base()
    if get_current_admin(request):
        return RedirectResponse(f"{base}/dashboard", status_code=303)  # type: ignore[return-value]
    return pages.login_html(base)


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse:
    base = _base()
    ip = _client_ip(request)

    if ratelimit.is_locked(ip):
        return pages.login_html(base, "Too many attempts. Try again later.", status_code=429)

    valid = username == settings.BACKOFFICE_ADMIN_USERNAME and verify_password(
        password, settings.BACKOFFICE_ADMIN_PASSWORD_HASH
    )
    if not valid:
        ratelimit.record_fail(ip)
        return pages.login_html(base, "Invalid credentials.", status_code=401)

    ratelimit.reset(ip)
    token = cp_session.issue_session(username)
    resp = RedirectResponse(f"{base}/dashboard", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.BACKOFFICE_SESSION_MAX_AGE_MINUTES * 60,
        httponly=True,
        secure=settings.backoffice_cookie_secure,
        samesite="strict",
        path=base,
    )
    return resp  # type: ignore[return-value]


@router.post("/logout")
async def logout() -> RedirectResponse:
    base = _base()
    resp = RedirectResponse(f"{base}/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path=base)
    return resp


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    base = _base()
    admin = get_current_admin(request)
    if not admin:
        return RedirectResponse(f"{base}/login", status_code=303)  # type: ignore[return-value]
    return pages.dashboard_html(base, admin)
