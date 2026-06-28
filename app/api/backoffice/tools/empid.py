"""EMPID Reconciliation — Backoffice 첫 도구 (업로드 → 버킷 리뷰 → 확정).

org 권한 밖. 세션쿠키 인증만. 단일 org 기준(멀티-org 시 org 선택 추가 — TODO).
"""

import html as _html
from uuid import UUID

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Depends

from app.api.backoffice import pages
from app.api.backoffice.deps import get_current_admin
from app.config import settings
from app.database import get_db
from app.models.organization import Organization
from app.services import empid_reconcile_service as svc

router: APIRouter = APIRouter(prefix="/tools/empid", include_in_schema=False)


def _base() -> str:
    return "/" + settings.BACKOFFICE_PATH.strip("/")


async def _get_org(db: AsyncSession) -> Organization | None:
    """단일 org 컨텍스트 (현재 멀티-org 비활성). 멀티-org 시 선택 UI로 교체."""
    return (
        await db.execute(
            select(Organization).where(Organization.is_active == True).order_by(Organization.created_at)  # noqa: E712
        )
    ).scalars().first()


def _esc(v: object) -> str:
    return _html.escape(str(v if v is not None else ""))


# --------------------------------------------------------------------------- #
# Upload form
# --------------------------------------------------------------------------- #
@router.get("", response_class=HTMLResponse)
async def upload_form(request: Request) -> HTMLResponse:
    admin = get_current_admin(request)
    base = _base()
    if not admin:
        return RedirectResponse(f"{base}/login", status_code=303)  # type: ignore[return-value]
    content = (
        "<div class='section'><div class='muted-box'>"
        "Upload the legacy employee list (<b>.xlsx</b> or <b>.csv</b>) with columns "
        "<code>COMPANY, CORP_ABR_3, Name, emp_id, Email</code>.<br>"
        "Matched by email against this org's users. PURADAK rows excluded. "
        "Already-assigned users are skipped (idempotent)."
        "</div></div>"
        "<div class='section'>"
        f"<form method='post' action='{base}/tools/empid/preview' enctype='multipart/form-data'>"
        "<input type='file' name='file' accept='.xlsx,.csv' required "
        "style='margin-bottom:16px;color:#e8e8ec'>"
        "<button type='submit' style='width:auto;padding:10px 20px'>Preview matches</button>"
        "</form></div>"
    )
    return pages.shell(base, admin, "/tools/empid", "EMPID Reconciliation", content)


# --------------------------------------------------------------------------- #
# Preview (parse + match + render review form)
# --------------------------------------------------------------------------- #
def _summary(counts: dict) -> str:
    chips = [
        ("auto", "Auto-assign", "#00b894"),
        ("multiple", "Multiple #", "#fdcb6e"),
        ("placeholder", "Placeholder", "#636e72"),
        ("assigned", "Already set", "#636e72"),
        ("deferred", "Deferred", "#636e72"),
        ("excluded_rows", "Excluded(PURADAK)", "#636e72"),
        ("total_rows", "Rows", "#8b7df0"),
    ]
    cells = "".join(
        f"<span style='display:inline-block;margin:0 12px 8px 0;padding:6px 12px;"
        f"border-radius:6px;background:{c}22;color:{c};font-size:13px'>"
        f"<b>{counts.get(k, 0)}</b> {label}</span>"
        for k, label, c in chips
    )
    return f"<div class='section'>{cells}</div>"


def _auto_table(props) -> str:
    if not props:
        return ""
    rows = "".join(
        f"<tr><td><input type='checkbox' name='assign' value='{_esc(p.user_id)}|{_esc(p.emp_id)}' checked></td>"
        f"<td>{_esc(p.user_full_name)}</td><td>{_esc(p.email)}</td>"
        f"<td><b>{_esc(p.emp_id)}</b></td></tr>"
        for p in props
    )
    return (
        f"<h3 style='color:#00b894'>Auto-assignable ({len(props)})</h3>"
        "<table style='width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px'>"
        "<tr style='text-align:left;color:#9a9ab0'><th>✓</th><th>User</th><th>Email</th><th>emp_id</th></tr>"
        f"{rows}</table>"
    )


def _multiple_table(props) -> str:
    if not props:
        return ""
    rows = ""
    for p in props:
        opts = "<option value=''>— skip —</option>" + "".join(
            f"<option value='{_esc(p.user_id)}|{_esc(e)}'>{_esc(e)}</option>" for e in p.emp_id_options
        )
        rows += (
            f"<tr><td>{_esc(p.user_full_name)}</td><td>{_esc(p.email)}</td>"
            f"<td><select name='assign' style='padding:4px;background:#0f0f17;color:#e8e8ec;"
            f"border:1px solid #2a2a40;border-radius:4px'>{opts}</select></td></tr>"
        )
    return (
        f"<h3 style='color:#fdcb6e'>Multiple numbers — pick canonical ({len(props)})</h3>"
        "<table style='width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px'>"
        "<tr style='text-align:left;color:#9a9ab0'><th>User</th><th>Email</th><th>Pick emp_id</th></tr>"
        f"{rows}</table>"
    )


def _readonly_table(title: str, color: str, props, show_options: bool = False) -> str:
    if not props:
        return ""
    rows = "".join(
        f"<tr><td>{_esc(p.name or p.user_full_name)}</td><td>{_esc(p.email)}</td>"
        f"<td>{_esc(', '.join(p.emp_id_options) if show_options and p.emp_id_options else (p.emp_id or ''))}</td>"
        f"<td style='color:#7a7a90'>{_esc(p.note)}</td></tr>"
        for p in props
    )
    return (
        f"<h3 style='color:{color}'>{_html.escape(title)} ({len(props)})</h3>"
        "<table style='width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px'>"
        "<tr style='text-align:left;color:#9a9ab0'><th>Name</th><th>Email</th><th>emp_id</th><th>note</th></tr>"
        f"{rows}</table>"
    )


@router.post("/preview", response_class=HTMLResponse)
async def preview(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    admin = get_current_admin(request)
    base = _base()
    if not admin:
        return RedirectResponse(f"{base}/login", status_code=303)  # type: ignore[return-value]

    org = await _get_org(db)
    if org is None:
        return pages.shell(base, admin, "/tools/empid", "EMPID Reconciliation",
                           "<div class='section'><div class='muted-box'>No active organization found.</div></div>")

    content_bytes = await file.read()
    result = await svc.reconcile(db, org.id, content_bytes, file.filename or "")

    deferred_note = (
        "<div class='muted-box' style='margin-bottom:24px'>"
        f"<b>Deferred / Placeholder</b> are report-only (not assigned). Org: <b>{_esc(org.name)}</b>, "
        f"file: <b>{_esc(file.filename)}</b>.</div>"
    )
    body = (
        _summary(result.counts())
        + deferred_note
        + f"<form method='post' action='{base}/tools/empid/commit'>"
        + _auto_table(result.auto)
        + _multiple_table(result.multiple)
        + "<button type='submit' style='width:auto;padding:11px 22px;margin-bottom:28px'>"
          "Confirm assignments</button></form>"
        + _readonly_table("Already assigned (skip)", "#636e72", result.assigned)
        + _readonly_table("Placeholder emails (excluded)", "#636e72", result.placeholder, show_options=True)
        + _readonly_table("Deferred (no DB match / no email)", "#636e72", result.deferred)
    )
    return pages.shell(base, admin, "/tools/empid", "EMPID — Review matches", body)


# --------------------------------------------------------------------------- #
# Commit
# --------------------------------------------------------------------------- #
@router.post("/commit", response_class=HTMLResponse)
async def commit(
    request: Request,
    assign: list[str] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    admin = get_current_admin(request)
    base = _base()
    if not admin:
        return RedirectResponse(f"{base}/login", status_code=303)  # type: ignore[return-value]

    org = await _get_org(db)
    if org is None:
        return pages.shell(base, admin, "/tools/empid", "EMPID Reconciliation",
                           "<div class='section'><div class='muted-box'>No active organization found.</div></div>")

    # "uid|empid" 파싱 (빈 select 값 제외)
    assignments: list[tuple[UUID, str]] = []
    for raw in assign:
        if not raw or "|" not in raw:
            continue
        uid_s, emp = raw.split("|", 1)
        try:
            assignments.append((UUID(uid_s), emp))
        except ValueError:
            continue

    res = await svc.commit_assignments(db, org.id, assignments)

    def _list(title: str, color: str, items, fmt) -> str:
        if not items:
            return ""
        lis = "".join(f"<li>{fmt(x)}</li>" for x in items)
        return f"<h3 style='color:{color}'>{_html.escape(title)} ({len(items)})</h3><ul style='font-size:13px'>{lis}</ul>"

    body = (
        f"<div class='section'><div class='muted-box'>Committed to org <b>{_esc(org.name)}</b>. "
        "Re-running is safe (already-assigned users are skipped).</div></div>"
        + _list("Assigned", "#00b894", res.assigned, lambda x: f"{_esc(x[0])} → <b>{_esc(x[1])}</b>")
        + _list("Skipped (already had emp_id)", "#636e72", res.skipped, lambda x: f"{_esc(x[0])} ({_esc(x[1])})")
        + _list("Rejected", "#ff8787", res.rejected, lambda x: f"{_esc(x[0])} — {_esc(x[1])}")
        + f"<div class='section'><a href='{base}/tools/empid'>← Back to EMPID</a></div>"
    )
    return pages.shell(base, admin, "/tools/empid", "EMPID — Commit result", body)
