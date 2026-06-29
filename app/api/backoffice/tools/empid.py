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
_STATS = [
    ("auto", "Auto-assign", "#1aae39"),
    ("multiple", "Multiple #", "#dd5b00"),
    ("mismatch", "Mismatch", "#c0392b"),
    ("placeholder", "Placeholder", "#615d59"),
    ("assigned", "Already set", "#615d59"),
    ("deferred", "Deferred", "#615d59"),
    ("excluded_rows", "Excluded", "#615d59"),
    ("total_rows", "Rows", "#0075de"),
]


def _summary(counts: dict) -> str:
    cells = "".join(
        f"<div class='stat'><div class='n' style='color:{c}'>{counts.get(k, 0)}</div>"
        f"<div class='l'>{label}</div></div>"
        for k, label, c in _STATS
    )
    return f"<div class='stats'>{cells}</div>"


def _bucket(anchor: str, title: str, color: str, count: int, body: str,
            sub: str = "", collapsed: bool = False) -> str:
    """버킷 카드 — 색 배지 + 제목 + (옵션)부제 + 본문. collapsed면 <details>로 접음."""
    head_inner = (
        f"<span class='badge' style='background:{color}'>{count}</span>"
        f"<span>{_html.escape(title)}</span>"
        + (f"<span class='bsub'>{sub}</span>" if sub else "")
    )
    if collapsed:
        return (
            f"<details id='{anchor}' class='bucket'>"
            f"<summary>{head_inner}</summary>"
            f"<div class='bbody'>{body}</div></details>"
        )
    return (
        f"<section id='{anchor}' class='bucket'>"
        f"<div class='bhead' style='color:{color}'>{head_inner}</div>"
        f"<div class='bbody'>{body}</div></section>"
    )


def _auto_table(props) -> str:
    if not props:
        return ""
    rows = "".join(
        f"<tr><td><input type='checkbox' name='assign' value='{_esc(p.user_id)}|{_esc(p.emp_id)}' checked></td>"
        f"<td>{_esc(p.user_full_name)}</td><td>{_esc(p.email)}</td>"
        f"<td><span class='pill'>{_esc(p.emp_id)}</span></td></tr>"
        for p in props
    )
    body = (
        "<table class='dtable'><thead><tr><th style='width:34px'>✓</th><th>User</th>"
        "<th>Email</th><th style='width:120px'>emp_id</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    return _bucket("auto", "Auto-assignable", "#1aae39", len(props), body,
                   sub="email matched · single number · empty in DB")


def _sources_html(p, warn: bool = False) -> str:
    """각 emp_id가 어느 COMPANY에서 왔는지 — 모든 번호를 한 번에 표시."""
    cls = "pill pill-warn" if warn else "pill"
    if not p.emp_id_sources:
        ids = p.emp_id_options if p.emp_id_options else ([p.emp_id] if p.emp_id else [])
        return " ".join(f"<span class='{cls}'>{_esc(e)}</span>" for e in ids)
    return "<div style='display:flex;flex-direction:column;gap:4px'>" + "".join(
        f"<div><span class='{cls}'>{_esc(eid)}</span> "
        f"<span class='hint'>← {_esc(co)}</span></div>"
        for eid, co in p.emp_id_sources
    ) + "</div>"


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
            f"<td><select class='sel' name='assign'>{opts}</select></td></tr>"
        )
    body = (
        "<table class='dtable'><thead><tr><th>User</th><th>Email</th>"
        "<th>Numbers &amp; source</th><th style='width:300px'>Pick canonical</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    return _bucket("multiple", "Multiple numbers — pick canonical", "#dd5b00", len(props), body,
                   sub="same person across stores · all numbers shown with their COMPANY")


def _mismatch_table(props) -> str:
    """이미 사번이 있는데 파일의 번호가 다른 경우 — 충돌, 운영자 확인용(읽기전용)."""
    if not props:
        return ""
    rows = "".join(
        f"<tr><td>{_esc(p.user_full_name or p.name)}</td>"
        f"<td>{_esc(p.email)}</td>"
        f"<td><span class='pill pill-warn'>{_esc(p.db_emp_id)}</span></td>"
        f"<td>{_sources_html(p, warn=True)}</td></tr>"
        for p in props
    )
    body = (
        "<div class='hint' style='margin-bottom:10px'>Already have an emp_id in the DB, but the file lists a "
        "<b>different</b> number. Not auto-changed — review manually (commit never overwrites).</div>"
        "<table class='dtable'><thead><tr><th>User</th><th>Email</th>"
        "<th style='width:120px'>DB emp_id</th><th>File says (← COMPANY)</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    return _bucket("mismatch", "Mismatch — DB vs file differ", "#c0392b", len(props), body)


def _placeholder_table(props) -> str:
    """더미/공유 이메일 — 같은 이메일을 쓰는 각 인물+번호 + 실제 DB 계정을 함께 표시(읽기전용)."""
    if not props:
        return ""
    rows = ""
    for p in props:
        # 각 인물 + 그 사람의 번호(+회사) — 이름 하나로 뭉뚱그리지 않음
        if p.members:
            people = "<div style='display:flex;flex-direction:column;gap:4px'>" + "".join(
                f"<div>{_esc(name)} <span class='pill'>{_esc(eid)}</span> "
                f"<span class='hint'>← {_esc(co)}</span></div>"
                for name, eid, co in p.members
            ) + "</div>"
        else:
            people = f"{_esc(p.name)} {_sources_html(p)}"
        # 이 이메일을 실제로 쓰는 DB 계정
        if p.db_accounts:
            db = "<div style='display:flex;flex-direction:column;gap:3px'>" + "".join(
                f"<div>{_esc(fn)}"
                + (f" <span class='pill'>{_esc(emp)}</span>" if emp else " <span class='hint'>(no emp_id)</span>")
                + "</div>"
                for fn, emp in p.db_accounts
            ) + "</div>"
        else:
            db = "<span style='color:#bdb9b4'>— none in DB —</span>"
        rows += (
            f"<tr><td>{_esc(p.email)}</td><td>{people}</td><td>{db}</td>"
            f"<td class='hint'>{_esc(p.note)}</td></tr>"
        )
    body = (
        "<div class='hint' style='margin-bottom:10px'>One email shared by several people (or an internal "
        "placeholder). Each person and their number is listed. <b>DB account</b> shows who actually uses "
        "that email in the server — match manually if needed.</div>"
        "<table class='dtable'><thead><tr><th style='width:220px'>Email</th>"
        "<th>People in file (name · number ← COMPANY)</th>"
        "<th>DB account(s) using this email</th><th>note</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    return _bucket("placeholder", "Placeholder / shared emails", "#615d59", len(props), body,
                   sub="one email, multiple people · shows each person+number and the real DB account")


def _assigned_card(props) -> str:
    """이미 배정(파일과 일치) — 노이즈라 기본 접힘."""
    if not props:
        return ""
    rows = "".join(
        f"<tr><td>{_esc(p.user_full_name or p.name)}</td><td>{_esc(p.email)}</td>"
        f"<td><span class='pill'>{_esc(p.emp_id)}</span></td></tr>"
        for p in props
    )
    body = (
        "<div class='hint' style='margin-bottom:10px'>Already have an emp_id that matches the file — nothing to do.</div>"
        "<table class='dtable'><thead><tr><th>User</th><th>Email</th>"
        "<th style='width:120px'>emp_id</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    return _bucket("assigned", "Already assigned (matches file)", "#615d59", len(props), body,
                   collapsed=True)


def _deferred_table(props) -> str:
    """DB 미매칭/무이메일 — 이름이 비슷한 DB 유저 후보를 힌트로 함께 표시."""
    if not props:
        return ""
    rows = ""
    for p in props:
        sim = (
            "<div style='display:flex;flex-direction:column;gap:3px'>" + "".join(
                f"<div>{_esc(n)} <span class='hint'>&lt;{_esc(e or '-')}&gt;</span></div>"
                for n, e in p.similar
            ) + "</div>"
            if p.similar else "<span style='color:#bdb9b4'>— none —</span>"
        )
        rows += (
            f"<tr><td>{_esc(p.name or p.user_full_name)}</td><td>{_esc(p.email)}</td>"
            f"<td>{_sources_html(p)}</td>"
            f"<td>{sim}</td><td class='hint'>{_esc(p.note)}</td></tr>"
        )
    body = (
        "<div class='hint' style='margin-bottom:10px'>Report-only. <b>Similar DB users</b> are name-based "
        "hints to help manual matching — verify before acting.</div>"
        "<table class='dtable'><thead><tr><th>Name (file)</th><th>Email</th>"
        "<th>emp_id</th><th>Similar DB users</th><th>note</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    return _bucket("deferred", "Deferred — no DB match / no email", "#615d59", len(props), body)


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

    # 공유용 Excel(.xlsx) — 버킷별 시트로 보기 편하게. data URI로 임베드(서버 상태 불필요)
    xlsx_bytes = svc.build_report_xlsx(result, org_name=org.name or "", filename=file.filename or "")
    xlsx_b64 = base64.b64encode(xlsx_bytes).decode("ascii")
    safe_org = "".join(c if c.isalnum() else "-" for c in (org.name or "org")).strip("-").lower() or "org"
    xlsx_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    # 비어있지 않은 버킷만 빠른 이동 링크로
    counts = result.counts()
    jump_meta = [
        ("auto", "Auto", "#1aae39"), ("multiple", "Multiple", "#dd5b00"),
        ("mismatch", "Mismatch", "#c0392b"), ("placeholder", "Placeholder", "#615d59"),
        ("deferred", "Deferred", "#615d59"), ("assigned", "Assigned", "#615d59"),
    ]
    jumps = "".join(
        f"<a href='#{k}'>{label} <b style='color:{c}'>{counts.get(k, 0)}</b></a>"
        for k, label, c in jump_meta if counts.get(k, 0)
    )

    toolbar = (
        "<div class='toolbar'>"
        f"<a class='btn-dl' download='empid-report-{safe_org}.xlsx' "
        f"href='data:{xlsx_mime};base64,{xlsx_b64}'>⬇ Download Excel report</a>"
        f"<div class='jump'>{jumps}</div>"
        "</div>"
    )

    note = (
        "<div class='muted-box section'>"
        f"Org <b>{_esc(org.name)}</b> · file <b>{_esc(file.filename)}</b>. "
        "<b>Mismatch / Placeholder / Deferred</b> are report-only (not assigned). "
        "Excel report has one sheet per bucket.</div>"
    )

    # 액션 가능한 항목(auto/multiple)을 commit 폼으로 묶음
    actionable = _auto_table(result.auto) + _multiple_table(result.multiple)
    if actionable:
        action_block = (
            f"<form method='post' action='{base}/tools/empid/commit'>"
            + actionable
            + "<div class='confirm-bar'><button type='submit'>Confirm assignments</button>"
            "<span class='hint'>Only checked / selected rows are written. Re-running is safe.</span></div>"
            "</form>"
        )
    else:
        action_block = (
            "<div class='muted-box section'>No auto-assignable or multiple-number matches in this file.</div>"
        )

    body = (
        _summary(counts)
        + toolbar
        + note
        + action_block
        + _mismatch_table(result.mismatch)
        + _placeholder_table(result.placeholder)
        + _deferred_table(result.deferred)
        + _assigned_card(result.assigned)
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
