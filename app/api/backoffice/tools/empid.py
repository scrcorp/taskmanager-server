"""EMPID Reconciliation — Backoffice 첫 도구 (업로드 → 버킷 리뷰 → 확정).

org 권한 밖. 세션쿠키 인증만. 단일 org 기준(멀티-org 시 org 선택 추가 — TODO).
"""

import base64
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
        "style='margin-bottom:16px;color:#1a1a1a'>"
        "<button type='submit' style='width:auto;padding:10px 20px'>Preview matches</button>"
        "</form></div>"
    )
    return pages.shell(base, admin, "/tools/empid", "EMPID Reconciliation", content)


# --------------------------------------------------------------------------- #
# Preview (parse + match + render review form)
# --------------------------------------------------------------------------- #
def _summary(counts: dict) -> str:
    chips = [
        ("auto", "Auto-assign", "#1aae39"),
        ("multiple", "Multiple #", "#dd5b00"),
        ("mismatch", "Mismatch", "#c0392b"),
        ("placeholder", "Placeholder", "#615d59"),
        ("assigned", "Already set", "#615d59"),
        ("deferred", "Deferred", "#615d59"),
        ("excluded_rows", "Excluded(PURADAK)", "#615d59"),
        ("total_rows", "Rows", "#0075de"),
    ]
    cells = "".join(
        f"<span style='display:inline-block;margin:0 12px 8px 0;padding:6px 12px;"
        f"border-radius:8px;background:{c}1a;color:{c};font-size:13px;font-weight:500'>"
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
        f"<h3 style='color:#1aae39'>Auto-assignable ({len(props)})</h3>"
        "<table style='width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px'>"
        "<tr style='text-align:left;color:#615d59'><th>✓</th><th>User</th><th>Email</th><th>emp_id</th></tr>"
        f"{rows}</table>"
    )


def _sources_html(p) -> str:
    """각 emp_id가 어느 COMPANY에서 왔는지 — 모든 번호를 한 번에 표시."""
    if not p.emp_id_sources:
        return _esc(", ".join(p.emp_id_options) if p.emp_id_options else (p.emp_id or ""))
    return "<br>".join(
        f"<b>{_esc(eid)}</b> <span style='color:#615d59'>← {_esc(co)}</span>"
        for eid, co in p.emp_id_sources
    )


def _multiple_table(props) -> str:
    if not props:
        return ""
    rows = ""
    for p in props:
        # 드롭다운 옵션에도 출처 회사를 함께 표기
        src_by_id = {eid: co for eid, co in p.emp_id_sources}
        opts = "<option value=''>— skip —</option>" + "".join(
            f"<option value='{_esc(p.user_id)}|{_esc(e)}'>{_esc(e)}"
            f"{(' — ' + _esc(src_by_id[e])) if src_by_id.get(e) else ''}</option>"
            for e in p.emp_id_options
        )
        rows += (
            f"<tr><td>{_esc(p.user_full_name)}</td><td>{_esc(p.email)}</td>"
            f"<td>{_sources_html(p)}</td>"
            f"<td><select name='assign' style='padding:5px 8px;background:#fff;color:#1a1a1a;"
            f"border:1px solid #ddd;border-radius:6px;font-family:inherit'>{opts}</select></td></tr>"
        )
    return (
        f"<h3 style='color:#dd5b00'>Multiple numbers — pick canonical ({len(props)})</h3>"
        "<div class='muted-box' style='margin-bottom:8px'>Same person across stores — each number "
        "and the COMPANY it came from is shown. Pick the canonical emp_id.</div>"
        "<table style='width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px'>"
        "<tr style='text-align:left;color:#615d59'><th>User</th><th>Email</th>"
        "<th>Numbers (← COMPANY)</th><th>Pick emp_id</th></tr>"
        f"{rows}</table>"
    )


def _mismatch_table(props) -> str:
    """이미 사번이 있는데 파일의 번호가 다른 경우 — 충돌, 운영자 확인용(읽기전용)."""
    if not props:
        return ""
    rows = "".join(
        f"<tr style='background:#c0392b0d'><td>{_esc(p.user_full_name or p.name)}</td>"
        f"<td>{_esc(p.email)}</td>"
        f"<td><b style='color:#c0392b'>{_esc(p.db_emp_id)}</b></td>"
        f"<td>{_sources_html(p)}</td></tr>"
        for p in props
    )
    return (
        f"<h3 style='color:#c0392b'>Mismatch — DB vs file differ ({len(props)})</h3>"
        "<div class='muted-box' style='margin-bottom:8px'>These users already have an emp_id in the DB, "
        "but the uploaded file lists a <b>different</b> number. Not auto-changed — review manually "
        "(commit never overwrites an existing emp_id).</div>"
        "<table style='width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px'>"
        "<tr style='text-align:left;color:#615d59'><th>User</th><th>Email</th>"
        "<th>DB emp_id</th><th>File says (← COMPANY)</th></tr>"
        f"{rows}</table>"
    )


def _readonly_table(title: str, color: str, props, show_options: bool = False) -> str:
    if not props:
        return ""
    rows = "".join(
        f"<tr><td>{_esc(p.name or p.user_full_name)}</td><td>{_esc(p.email)}</td>"
        f"<td>{_esc(', '.join(p.emp_id_options) if show_options and p.emp_id_options else (p.emp_id or ''))}</td>"
        f"<td style='color:#615d59'>{_esc(p.note)}</td></tr>"
        for p in props
    )
    return (
        f"<h3 style='color:{color}'>{_html.escape(title)} ({len(props)})</h3>"
        "<table style='width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px'>"
        "<tr style='text-align:left;color:#615d59'><th>Name</th><th>Email</th><th>emp_id</th><th>note</th></tr>"
        f"{rows}</table>"
    )


def _deferred_table(props) -> str:
    """DB 미매칭/무이메일 — 이름이 비슷한 DB 유저 후보를 힌트로 함께 표시."""
    if not props:
        return ""
    rows = ""
    for p in props:
        sim = (
            "<br>".join(
                f"{_esc(n)} <span style='color:#615d59'>&lt;{_esc(e or '-')}&gt;</span>"
                for n, e in p.similar
            )
            if p.similar else "<span style='color:#a8a29e'>— none —</span>"
        )
        rows += (
            f"<tr><td>{_esc(p.name or p.user_full_name)}</td><td>{_esc(p.email)}</td>"
            f"<td>{_esc(', '.join(p.emp_id_options) if p.emp_id_options else (p.emp_id or ''))}</td>"
            f"<td>{sim}</td><td style='color:#615d59'>{_esc(p.note)}</td></tr>"
        )
    return (
        f"<h3 style='color:#615d59'>Deferred — no DB match / no email ({len(props)})</h3>"
        "<div class='muted-box' style='margin-bottom:8px'>Report-only. "
        "<b>Similar DB users</b> are name-based hints to help manual matching — verify before acting.</div>"
        "<table style='width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px'>"
        "<tr style='text-align:left;color:#615d59'><th>Name (file)</th><th>Email</th>"
        "<th>emp_id</th><th>Similar DB users</th><th>note</th></tr>"
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

    # 공유용 CSV — 페이지에 data URI 다운로드 링크로 바로 임베드(서버 상태 불필요)
    csv_text = svc.build_report_csv(result)
    csv_b64 = base64.b64encode(csv_text.encode("utf-8")).decode("ascii")
    safe_org = "".join(c if c.isalnum() else "-" for c in (org.name or "org")).strip("-").lower() or "org"
    download = (
        "<div class='section'>"
        f"<a download='empid-report-{safe_org}.csv' "
        f"href='data:text/csv;charset=utf-8;base64,{csv_b64}' "
        "style='display:inline-block;padding:9px 18px;background:#0075de;color:#fff;"
        "border-radius:8px;text-decoration:none;font-size:13px;font-weight:500'>"
        "⬇ Download CSV report</a>"
        "<span style='color:#615d59;font-size:12px;margin-left:12px'>"
        "All buckets (auto / multiple / mismatch / assigned / placeholder / deferred) for sharing.</span>"
        "</div>"
    )

    note = (
        "<div class='muted-box' style='margin-bottom:24px'>"
        f"Org: <b>{_esc(org.name)}</b>, file: <b>{_esc(file.filename)}</b>. "
        "<b>Mismatch / Placeholder / Deferred</b> are report-only (not assigned).</div>"
    )

    # 이미 배정된 인물은 기본적으로 접어 노이즈 제거(원하면 펼쳐서 확인)
    assigned_block = (
        "<details style='margin-bottom:24px'>"
        f"<summary style='cursor:pointer;color:#615d59;font-size:14px;font-weight:600'>"
        f"Already assigned — matches file (skip) ({len(result.assigned)})</summary>"
        + _readonly_table("", "#615d59", result.assigned)
        + "</details>"
    ) if result.assigned else ""

    body = (
        _summary(result.counts())
        + download
        + note
        + f"<form method='post' action='{base}/tools/empid/commit'>"
        + _auto_table(result.auto)
        + _multiple_table(result.multiple)
        + "<button type='submit' style='width:auto;padding:11px 22px;margin-bottom:28px'>"
          "Confirm assignments</button></form>"
        + _mismatch_table(result.mismatch)
        + _readonly_table("Placeholder emails (excluded)", "#615d59", result.placeholder, show_options=True)
        + _deferred_table(result.deferred)
        + assigned_block
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
        + _list("Assigned", "#1aae39", res.assigned, lambda x: f"{_esc(x[0])} → <b>{_esc(x[1])}</b>")
        + _list("Skipped (already had emp_id)", "#615d59", res.skipped, lambda x: f"{_esc(x[0])} ({_esc(x[1])})")
        + _list("Rejected", "#c0392b", res.rejected, lambda x: f"{_esc(x[0])} — {_esc(x[1])}")
        + f"<div class='section'><a href='{base}/tools/empid'>← Back to EMPID</a></div>"
    )
    return pages.shell(base, admin, "/tools/empid", "EMPID — Commit result", body)
