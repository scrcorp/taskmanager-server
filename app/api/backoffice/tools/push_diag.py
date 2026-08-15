"""Push 진단 — Backoffice 도구 (읽기 전용 조회).

"알림이 안 왔다" 의 답을 찾는 화면. 발송 기능은 없다 — prod 에서 실제 직원 폰으로
나가는 도구는 오발송이 곧 피해라서, 먼저 **조회만** 둔다.

답해야 할 질문과 근거 테이블:

    이 사람 기기가 등록돼 있나        → push_subscriptions
    보냈나, 왜 안 보냈나              → push_deliveries (skipped + skip_reason)
    본인이 그 카테고리를 껐나          → alert_preference_audits

`sent` 는 "중계 서버가 받아줬다" 까지다. 폰 화면에 떴는지는 서버가 알 수 없다
(OS 알림 설정이 꺼져 있으면 accepted 여도 안 보인다). 화면에서 그 한계를 명시한다.

org 권한 밖 — 세션쿠키 인증만(get_current_admin). 다른 도구와 동일.
"""

import html as _html
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.backoffice import pages
from app.api.backoffice.deps import get_current_admin
from app.config import settings
from app.database import get_db
from app.models.alert_preference_audit import AlertPreferenceAudit
from app.models.organization import Organization
from app.models.push_delivery import PushDelivery
from app.models.push_subscription import PushSubscription
from app.models.user import User

router: APIRouter = APIRouter(prefix="/tools/push", include_in_schema=False)

_ACTIVE = "/tools/push"

# 한 화면에 담을 최대 행 수. 넘으면 잘렸다고 화면에 표시한다 —
# 조용히 자르면 "이력이 이것뿐" 으로 잘못 읽힌다.
_SEARCH_LIMIT = 30
_DELIVERY_LIMIT = 50
_AUDIT_LIMIT = 30


def _base() -> str:
    return "/" + settings.BACKOFFICE_PATH.strip("/")


def _esc(v: object) -> str:
    return _html.escape(str(v if v is not None else ""))


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _ago(ts: Optional[datetime]) -> str:
    """상대 시각 — 절대 시각만 있으면 '오래된 건지' 가 한눈에 안 들어온다."""
    if ts is None:
        return "—"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 0:
        return ts.strftime("%Y-%m-%d %H:%M")
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _stamp(ts: Optional[datetime]) -> str:
    if ts is None:
        return "—"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.strftime("%Y-%m-%d %H:%M:%S UTC")


def _pref_value(value: Optional[bool]) -> str:
    """3-상태 — None 은 '미설정(기본 on)' 이고 False 와 다르다."""
    if value is None:
        return "<span class='muted'>unset</span>"
    return "<b>on</b>" if value else "<b style='color:#b3261e'>off</b>"


# --------------------------------------------------------------------------- #
# 사용자 검색
# --------------------------------------------------------------------------- #
@router.get("", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = Query("", max_length=100),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    admin = get_current_admin(request)
    base = _base()
    if not admin:
        return _redirect(f"{base}/login")

    term = q.strip()
    rows_html = ""
    truncated = False

    if term:
        pattern = f"%{term}%"
        stmt = (
            select(User, Organization)
            .join(Organization, Organization.id == User.organization_id, isouter=True)
            .where(
                or_(
                    User.username.ilike(pattern),
                    User.full_name.ilike(pattern),
                )
            )
            .order_by(User.username)
            .limit(_SEARCH_LIMIT + 1)
        )
        found = (await db.execute(stmt)).all()
        truncated = len(found) > _SEARCH_LIMIT
        found = found[:_SEARCH_LIMIT]

        if not found:
            rows_html = "<tr><td colspan='4' class='muted'>No user matched.</td></tr>"
        else:
            for user, org in found:
                rows_html += (
                    "<tr>"
                    f"<td><a href='{base}{_ACTIVE}/{user.id}'>{_esc(user.username)}</a></td>"
                    f"<td>{_esc(user.full_name)}</td>"
                    f"<td>{_esc(org.name if org else '—')}</td>"
                    f"<td><a href='{base}{_ACTIVE}/{user.id}'>Diagnose →</a></td>"
                    "</tr>"
                )

    note = ""
    if truncated:
        note = (
            f"<div class='msg err'>Showing first {_SEARCH_LIMIT} matches only. "
            "Narrow the search to see the rest.</div>"
        )

    table = ""
    if term:
        table = (
            f"{note}"
            "<table><thead><tr><th>Username</th><th>Name</th><th>Organization</th><th></th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table>"
        )

    content = (
        "<div class='section'>"
        "<div class='muted-box'>"
        "Read-only. Answers <b>&ldquo;why didn't this person get the notification?&rdquo;</b> "
        "from what the server already recorded. No notification is sent from this page."
        "</div>"
        f"<form method='get' action='{base}{_ACTIVE}'>"
        "<label>Search user (username or name)</label>"
        f"<input name='q' value='{_esc(term)}' autofocus autocomplete='off'>"
        "<button type='submit'>Search</button>"
        "</form>"
        f"{table}"
        "</div>"
    )
    return pages.shell(base, admin, _ACTIVE, "Push Diagnostics", content,
                       "HTM Backoffice — Push Diagnostics")


# --------------------------------------------------------------------------- #
# 사용자별 상세
# --------------------------------------------------------------------------- #
@router.get("/{user_id}", response_class=HTMLResponse)
async def detail_page(
    request: Request,
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    admin = get_current_admin(request)
    base = _base()
    if not admin:
        return _redirect(f"{base}/login")

    user = await db.get(User, user_id)
    if user is None:
        content = (
            "<div class='section'><div class='msg err'>User not found.</div>"
            f"<a href='{base}{_ACTIVE}'>← Back to search</a></div>"
        )
        return pages.shell(base, admin, _ACTIVE, "Push Diagnostics", content)

    org = await db.get(Organization, user.organization_id) if user.organization_id else None

    subs = (
        await db.execute(
            select(PushSubscription)
            .where(PushSubscription.user_id == user_id)
            .order_by(PushSubscription.created_at.desc())
        )
    ).scalars().all()

    deliveries = (
        await db.execute(
            select(PushDelivery)
            .where(PushDelivery.user_id == user_id)
            .order_by(PushDelivery.created_at.desc())
            .limit(_DELIVERY_LIMIT)
        )
    ).scalars().all()

    audits = (
        await db.execute(
            select(AlertPreferenceAudit)
            .where(AlertPreferenceAudit.user_id == user_id)
            .order_by(AlertPreferenceAudit.changed_at.desc())
            .limit(_AUDIT_LIMIT)
        )
    ).scalars().all()

    # ── 한 줄 진단 ──────────────────────────────────────────
    # 운영자가 표 세 개를 읽기 전에 결론부터 본다.
    if not settings.push_enabled:
        verdict_bad = True
        verdict_text = ("Push is disabled on this server (no VAPID keys). "
                        "Nothing can be delivered in this environment.")
    elif not subs:
        verdict_bad = True
        verdict_text = ("No device is registered. The user must turn push on from the app, "
                        "on the device they expect notifications on.")
    else:
        verdict_bad = False
        verdict_text = (f"{len(subs)} device(s) registered. "
                        "Check the delivery log below for what actually happened.")

    verdict_class = "msg err" if verdict_bad else "msg"
    verdict_style = "" if verdict_bad else " style='background:#eaf7ee;color:#14632c'"
    verdict_html = (
        f"<div class='{verdict_class}'{verdict_style}>{_esc(verdict_text)}</div>"
    )

    # ── 기기 ────────────────────────────────────────────────
    if subs:
        sub_rows = ""
        for s in subs:
            sub_rows += (
                "<tr>"
                f"<td title='{_esc(s.endpoint)}'>{_esc(s.endpoint[:48])}…</td>"
                f"<td>{_esc((s.user_agent or '—')[:60])}</td>"
                f"<td>{_esc(s.failure_count)}</td>"
                f"<td>{_esc(_ago(s.last_success_at))}</td>"
                f"<td>{_esc(_ago(s.created_at))}</td>"
                "</tr>"
            )
        subs_html = (
            "<table><thead><tr><th>Endpoint</th><th>User agent</th>"
            "<th>Failures</th><th>Last success</th><th>Registered</th></tr></thead>"
            f"<tbody>{sub_rows}</tbody></table>"
        )
    else:
        subs_html = "<div class='muted-box'>No push subscription rows for this user.</div>"

    # ── 발송 이력 ───────────────────────────────────────────
    if deliveries:
        del_rows = ""
        for d in deliveries:
            reason = f" <span class='muted'>({_esc(d.skip_reason)})</span>" if d.skip_reason else ""
            color = {
                "accepted": "#14632c",
                "skipped": "#8a6d00",
                "gone": "#b3261e",
                "failed": "#b3261e",
            }.get(d.status, "#333")
            del_rows += (
                "<tr>"
                f"<td>{_esc(_stamp(d.created_at))}</td>"
                f"<td><b style='color:{color}'>{_esc(d.status)}</b>{reason}</td>"
                f"<td>{_esc(d.alert_type or '—')}</td>"
                f"<td>{_esc(d.title or '—')}</td>"
                f"<td>{_esc((d.error or '')[:80]) or '—'}</td>"
                "</tr>"
            )
        trunc = ""
        if len(deliveries) == _DELIVERY_LIMIT:
            trunc = (f"<div class='muted'>Showing the most recent {_DELIVERY_LIMIT} "
                     "records only — older ones exist.</div>")
        deliveries_html = (
            "<table><thead><tr><th>When</th><th>Status</th><th>Alert type</th>"
            "<th>Title</th><th>Error</th></tr></thead>"
            f"<tbody>{del_rows}</tbody></table>{trunc}"
        )
    else:
        deliveries_html = (
            "<div class='muted-box'>No delivery attempt recorded. "
            "Either no alert targeted this user, or the alerts predate push being enabled.</div>"
        )

    # ── 설정 변경 이력 ──────────────────────────────────────
    # 남이 바꾼 경우 username 을 보여준다. UUID 를 그대로 찍으면 "누가 껐는지" 를
    # 증명하려고 만든 이력이 증거로 쓸 수 없는 값이 된다.
    actor_names: dict[UUID, str] = {}
    other_actor_ids = {
        a.changed_by_user_id for a in audits if a.changed_by_user_id != a.user_id
    }
    if other_actor_ids:
        rows = (
            await db.execute(
                select(User.id, User.username).where(User.id.in_(other_actor_ids))
            )
        ).all()
        actor_names = {r[0]: r[1] for r in rows}

    if audits:
        audit_rows = ""
        for a in audits:
            if a.changed_by_user_id == a.user_id:
                actor = "self"
            else:
                # 지워진 사용자면 이름이 없다 — 그때만 UUID 로 떨어진다.
                actor = _esc(actor_names.get(a.changed_by_user_id, a.changed_by_user_id))
            audit_rows += (
                "<tr>"
                f"<td>{_esc(_stamp(a.changed_at))}</td>"
                f"<td>{_esc(a.category_code)}</td>"
                f"<td>{_esc(a.channel)}</td>"
                f"<td>{_pref_value(a.old_value)} → {_pref_value(a.new_value)}</td>"
                f"<td>{actor}</td>"
                "</tr>"
            )
        audits_html = (
            "<table><thead><tr><th>When</th><th>Category</th><th>Channel</th>"
            "<th>Change</th><th>Changed by</th></tr></thead>"
            f"<tbody>{audit_rows}</tbody></table>"
        )
    else:
        audits_html = (
            "<div class='muted-box'>No preference change recorded — "
            "this user has never changed a notification setting (all defaults).</div>"
        )

    content = (
        "<div class='section'>"
        f"<a href='{base}{_ACTIVE}'>← Back to search</a>"
        f"<h2>{_esc(user.username)} <span class='muted'>· {_esc(user.full_name)}"
        f"{' · ' + _esc(org.name) if org else ''}</span></h2>"
        f"{verdict_html}"
        "</div>"
        "<div class='section'><h3>Registered devices</h3>"
        f"{subs_html}</div>"
        "<div class='section'><h3>Delivery log</h3>"
        "<div class='muted-box'>"
        "<b>accepted</b> means the relay (FCM/APNs) took it — <b>not</b> that the phone showed it. "
        "If the OS notification setting is off, an accepted push is silently invisible. "
        "<b>skipped</b> means the server chose not to send; the reason is in parentheses."
        "</div>"
        f"{deliveries_html}</div>"
        "<div class='section'><h3>Notification setting changes</h3>"
        "<div class='muted-box'>"
        "<b>unset</b> is not the same as <b>off</b> — unset means the user never touched it "
        "and the default (on) applied."
        "</div>"
        f"{audits_html}</div>"
    )
    return pages.shell(base, admin, _ACTIVE, "Push Diagnostics", content,
                       "HTM Backoffice — Push Diagnostics")
