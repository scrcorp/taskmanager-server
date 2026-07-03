"""Organizations 관리 — Backoffice 도구 (목록 + 신규 org 생성).

org 권한 밖, 세션쿠키 인증만(get_current_admin). 운영자가 새 고객사(organization)를
직접 생성한다: org + 5 roles + 권한 + 기본 템플릿 + super_owner(+org_member) + 첫 store.
생성 로직은 organization_service.create_organization 재사용.
"""

import html as _html

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.api.backoffice import pages
from app.api.backoffice.deps import get_current_admin
from app.config import settings
from app.database import get_db
from app.models.organization import Organization, Store
from app.models.user import Role, User
from app.services.organization_service import organization_service

router: APIRouter = APIRouter(prefix="/tools/orgs", include_in_schema=False)

_ACTIVE = "/tools/orgs"

_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "Asia/Seoul",
]


def _base() -> str:
    return "/" + settings.BACKOFFICE_PATH.strip("/")


def _esc(v: object) -> str:
    return _html.escape(str(v if v is not None else ""))


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


# --------------------------------------------------------------------------- #
# 목록
# --------------------------------------------------------------------------- #
@router.get("", response_class=HTMLResponse)
async def list_page(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    admin = get_current_admin(request)
    base = _base()
    if not admin:
        return _redirect(f"{base}/login")

    # org + 매장수 + super_owner username
    orgs = (await db.execute(select(Organization).order_by(Organization.created_at.desc()))).scalars().all()
    store_counts = dict(
        (await db.execute(
            select(Store.organization_id, func.count(Store.id))
            .where(Store.deleted_at.is_(None))
            .group_by(Store.organization_id)
        )).all()
    )
    su_rows = (await db.execute(
        select(User.organization_id, User.username)
        .join(Role, Role.id == User.role_id)
        .where(Role.priority == 5)
    )).all()
    su_map: dict = {}
    for oid, uname in su_rows:
        su_map.setdefault(oid, uname)

    rows = ""
    for o in orgs:
        rows += (
            f"<tr><td><b>{_esc(o.name)}</b></td>"
            f"<td><span class='pill'>{_esc(o.code)}</span></td>"
            f"<td>{_esc(su_map.get(o.id, '—'))}</td>"
            f"<td class='num'>{_esc(store_counts.get(o.id, 0))}</td>"
            f"<td>{_esc(o.timezone)}</td>"
            f"<td>{_esc(o.created_at.strftime('%Y-%m-%d') if o.created_at else '—')}</td></tr>"
        )
    if not rows:
        rows = "<tr><td colspan='6' class='empty'>No organizations yet.</td></tr>"

    content = (
        "<div class='toolbar'>"
        f"<a class='btn-dl' href='{base}/tools/orgs/new'>+ New Organization</a>"
        f"<span class='hint'>{len(orgs)} organizations</span>"
        "</div>"
        "<div class='bucket'><table class='dtable'>"
        "<thead><tr><th>Name</th><th>Code</th><th>Super Owner</th><th>Stores</th><th>Timezone</th><th>Created</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )
    return pages.shell(base, admin, _ACTIVE, "Organizations", content, "HTM Backoffice — Organizations")


# --------------------------------------------------------------------------- #
# 신규 생성 폼
# --------------------------------------------------------------------------- #
def _new_form(base: str, message: str = "") -> str:
    msg = f"<div class='msg err'>{_esc(message)}</div>" if message else ""
    tz_opts = "".join(f"<option value='{_esc(t)}'>{_esc(t)}</option>" for t in _TIMEZONES)
    return (
        f"{msg}"
        "<div class='muted-box' style='max-width:520px'>"
        f"<form method='post' action='{base}/tools/orgs/new'>"
        "<label>Organization Name *</label>"
        "<input name='name' required maxlength='255' placeholder='e.g. Sunrise Cafe Inc.'>"
        "<label>Timezone *</label>"
        f"<select name='timezone' required>{tz_opts}</select>"
        "<hr style='border:none;border-top:1px solid var(--hairline);margin:18px 0'>"
        "<label>Admin Username *</label>"
        "<input name='admin_username' required maxlength='100' placeholder='e.g. johnkim'>"
        "<label>Admin Password *</label>"
        "<input name='admin_password' type='password' required minlength='6'>"
        "<label>Admin Email (optional)</label>"
        "<input name='admin_email' type='email' maxlength='255'>"
        "<label>First Store Name (optional)</label>"
        "<input name='first_store_name' maxlength='255' placeholder='e.g. Downtown Branch'>"
        "<button type='submit' style='width:auto;padding:10px 22px;margin-top:8px'>Create Organization</button>"
        "</form></div>"
    )


@router.get("/new", response_class=HTMLResponse)
async def new_page(request: Request) -> HTMLResponse:
    admin = get_current_admin(request)
    base = _base()
    if not admin:
        return _redirect(f"{base}/login")
    return pages.shell(base, admin, _ACTIVE, "New Organization", _new_form(base), "HTM Backoffice — New Organization")


@router.post("/new", response_class=HTMLResponse)
async def new_submit(
    request: Request,
    name: str = Form(...),
    timezone: str = Form(...),
    admin_username: str = Form(...),
    admin_password: str = Form(...),
    admin_email: str = Form(""),
    first_store_name: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    admin = get_current_admin(request)
    base = _base()
    if not admin:
        return _redirect(f"{base}/login")

    try:
        res = await organization_service.create_organization(
            db,
            name=name.strip(),
            admin_username=admin_username.strip(),
            admin_password=admin_password,
            admin_email=(admin_email.strip() or None),
            timezone=timezone,
            first_store_name=(first_store_name.strip() or None),
        )
    except Exception as e:  # noqa: BLE001 — 운영자에게 원인 표시
        return pages.shell(base, admin, _ACTIVE, "New Organization",
                           _new_form(base, f"Failed: {e}"), "HTM Backoffice — New Organization")

    content = (
        "<div class='muted-box' style='max-width:520px'>"
        f"<h3 style='margin-top:0'>Organization created</h3>"
        f"<p><b>{_esc(res['name'])}</b> is ready.</p>"
        "<table class='dtable' style='margin-top:8px'>"
        f"<tr><td>Organization Code</td><td><span class='pill' style='font-size:15px'>{_esc(res['code'])}</span></td></tr>"
        f"<tr><td>Super Owner</td><td>{_esc(res['admin_username'])}</td></tr>"
        f"<tr><td>First Store</td><td>{'created' if res['store_id'] else '—'}</td></tr>"
        "</table>"
        "<p class='hint' style='margin-top:12px'>Share the code + admin credentials with the owner. "
        "They sign in at the console with the username &amp; password.</p>"
        f"<a class='btn-dl' href='{base}/tools/orgs' style='margin-top:12px'>← Back to Organizations</a>"
        "</div>"
    )
    return pages.shell(base, admin, _ACTIVE, "Organization Created", content, "HTM Backoffice — Organization Created")
