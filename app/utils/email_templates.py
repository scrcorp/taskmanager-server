"""Email HTML template builder."""

from datetime import date
from html import escape
from typing import TYPE_CHECKING
from urllib.parse import urlencode

if TYPE_CHECKING:
    from app.services.schedule_report_service import Issue, ReportDiff, ShiftCell, StoreInfo

_DOW_LABEL = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def build_verification_code_email(code: str) -> tuple[str, str]:
    """Build email verification code email.

    Returns:
        (subject, html_body) tuple
    """
    subject = "[Verification] Your code"

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background-color:#F8FAFC;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#F8FAFC;">
    <tr><td align="center" style="padding:32px 16px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background-color:#FFFFFF;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        <tr>
          <td style="background-color:#3B8DD9;padding:24px 28px;">
            <div style="font-size:22px;font-weight:700;color:#FFFFFF;">HTM</div>
          </td>
        </tr>
        <tr>
          <td style="padding:32px 24px;text-align:center;">
            <div style="font-size:20px;font-weight:700;color:#1E293B;margin-bottom:8px;">Email Verification Code</div>
            <div style="font-size:14px;color:#64748B;margin-bottom:28px;">Enter this code to verify your email address.</div>
            <div style="display:inline-block;padding:16px 40px;background-color:#F1F5F9;border-radius:8px;font-size:32px;font-weight:800;letter-spacing:8px;color:#1E293B;">{escape(code)}</div>
            <div style="font-size:13px;color:#94A3B8;margin-top:24px;">This code expires in 5 minutes.</div>
            <div style="font-size:13px;color:#94A3B8;margin-top:4px;">If you didn't request this, please ignore this email.</div>
          </td>
        </tr>
        <tr>
          <td style="padding:20px 24px;background-color:#F8FAFC;border-top:1px solid #E2E8F0;">
            <div style="font-size:13px;color:#94A3B8;text-align:center;">Automated alert from HTM</div>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    return subject, html


def build_daily_report_email(
    store_name: str,
    report_date: str,
    period: str,
    author_name: str,
    submitted_at: str,
    sections: list[dict],
) -> tuple[str, str]:
    """Build daily report submission alert email.

    Returns:
        (subject, html_body) tuple
    """
    period_label = "Lunch" if period == "lunch" else "Dinner"
    subject = f"[Daily Report] {store_name} - {report_date} {period_label} ({author_name})"

    sections_html = ""
    for s in sections:
        title = escape(s.get("title", ""))
        content = s.get("content") or ""
        content = content.strip()
        if not content:
            content_html = '<span style="color:#94A3B8;">No content</span>'
        else:
            content_html = escape(content).replace("\n", "<br>")
        sections_html += f"""
            <tr>
              <td style="padding:20px 24px;border-bottom:1px solid #E2E8F0;">
                <div style="font-size:15px;font-weight:600;color:#2563EB;margin-bottom:8px;">{title}</div>
                <div style="font-size:16px;color:#334155;line-height:1.7;">{content_html}</div>
              </td>
            </tr>"""

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background-color:#F8FAFC;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#F8FAFC;">
    <tr><td align="center" style="padding:32px 16px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background-color:#FFFFFF;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        <!-- Header -->
        <tr>
          <td style="background-color:#2563EB;padding:24px 28px;">
            <div style="font-size:22px;font-weight:700;color:#FFFFFF;">HTM</div>
          </td>
        </tr>
        <!-- Meta -->
        <tr>
          <td style="padding:28px 24px 20px;">
            <div style="font-size:20px;font-weight:700;color:#1E293B;margin-bottom:16px;">Daily Report Submitted</div>
            <table role="presentation" cellpadding="0" cellspacing="0" style="font-size:16px;color:#64748B;line-height:2;">
              <tr><td style="padding-right:16px;font-weight:600;">Store</td><td style="color:#1E293B;">{escape(store_name)}</td></tr>
              <tr><td style="padding-right:16px;font-weight:600;">Date</td><td style="color:#1E293B;">{escape(report_date)}</td></tr>
              <tr><td style="padding-right:16px;font-weight:600;">Period</td><td style="color:#1E293B;">{period_label}</td></tr>
              <tr><td style="padding-right:16px;font-weight:600;">Author</td><td style="color:#1E293B;">{escape(author_name)}</td></tr>
              <tr><td style="padding-right:16px;font-weight:600;">Submitted</td><td style="color:#1E293B;">{escape(submitted_at)}</td></tr>
            </table>
          </td>
        </tr>
        <!-- Divider -->
        <tr><td style="padding:0 24px;"><hr style="border:none;border-top:1px solid #E2E8F0;margin:0;"></td></tr>
        <!-- Sections -->
        <tr>
          <td style="padding:8px 0;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              {sections_html}
            </table>
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td style="padding:20px 24px;background-color:#F8FAFC;border-top:1px solid #E2E8F0;">
            <div style="font-size:13px;color:#94A3B8;text-align:center;">Automated alert from HTM</div>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    return subject, html


def build_checklist_completed_email(
    store_name: str,
    staff_name: str,
    work_role_name: str,
    work_date: str,
    template_name: str,
    total_items: int,
    completed_items: int,
    admin_url: str,
) -> tuple[str, str]:
    """Build checklist completion alert email.

    Returns:
        (subject, html_body) tuple
    """
    subject = f"[Checklist] {store_name} — {staff_name} completed ({work_date})"

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background-color:#F8FAFC;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#F8FAFC;">
    <tr><td align="center" style="padding:32px 16px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background-color:#FFFFFF;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        <!-- Header -->
        <tr>
          <td style="background-color:#00B894;padding:24px 28px;">
            <div style="font-size:22px;font-weight:700;color:#FFFFFF;">HTM</div>
          </td>
        </tr>
        <!-- Content -->
        <tr>
          <td style="padding:28px 24px 20px;">
            <div style="font-size:20px;font-weight:700;color:#1E293B;margin-bottom:16px;">Checklist Completed</div>
            <table role="presentation" cellpadding="0" cellspacing="0" style="font-size:16px;color:#64748B;line-height:2;">
              <tr><td style="padding-right:16px;font-weight:600;">Store</td><td style="color:#1E293B;">{escape(store_name)}</td></tr>
              <tr><td style="padding-right:16px;font-weight:600;">Staff</td><td style="color:#1E293B;">{escape(staff_name)}</td></tr>
              <tr><td style="padding-right:16px;font-weight:600;">Role</td><td style="color:#1E293B;">{escape(work_role_name)}</td></tr>
              <tr><td style="padding-right:16px;font-weight:600;">Date</td><td style="color:#1E293B;">{escape(work_date)}</td></tr>
              <tr><td style="padding-right:16px;font-weight:600;">Checklist</td><td style="color:#1E293B;">{escape(template_name)}</td></tr>
              <tr><td style="padding-right:16px;font-weight:600;">Items</td><td style="color:#1E293B;">{completed_items} / {total_items}</td></tr>
            </table>
          </td>
        </tr>
        <!-- CTA Button -->
        <tr>
          <td style="padding:8px 24px 28px;">
            <a href="{escape(admin_url)}" style="display:inline-block;padding:12px 32px;background-color:#6C5CE7;color:#FFFFFF;font-size:16px;font-weight:600;text-decoration:none;border-radius:6px;">Review Checklist</a>
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td style="padding:20px 24px;background-color:#F8FAFC;border-top:1px solid #E2E8F0;">
            <div style="font-size:13px;color:#94A3B8;text-align:center;">Automated alert from HTM</div>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    return subject, html


def build_reply_email(
    recipient_name: str,
    author_name: str,
    context_label: str,
    context_subtitle: str,
    excerpt: str | None,
    cta_url: str | None = None,
) -> tuple[str, str]:
    """Build a generic alert email for a reply on a checklist item or daily report.

    Args:
        recipient_name: 받는 사람 이름 (예: "Alice")
        author_name: 답변을 단 사람 이름 (관리자)
        context_label: "Checklist Item" 또는 "Daily Report"
        context_subtitle: 추가 식별자 (예: 항목 제목, 보고서 날짜)
        excerpt: 답변 내용 일부 (50~120자), None 가능 (사진/영상만 첨부된 경우)
        cta_url: 보러 갈 링크 (옵션)
    """
    subject = f"[Reply] {author_name} on {context_label.lower()}"
    excerpt_html = (
        f'<div style="margin-top:12px;padding:12px 16px;background-color:#F1F5F9;border-left:3px solid #6C5CE7;border-radius:4px;font-size:14px;color:#334155;line-height:1.6;">{escape(excerpt)}</div>'
        if excerpt and excerpt.strip()
        else '<div style="margin-top:12px;font-size:13px;color:#94A3B8;font-style:italic;">(Photo or video attachment)</div>'
    )
    cta_html = (
        f'<tr><td style="padding:8px 24px 28px;"><a href="{escape(cta_url)}" style="display:inline-block;padding:12px 32px;background-color:#6C5CE7;color:#FFFFFF;font-size:16px;font-weight:600;text-decoration:none;border-radius:6px;">Open in HTM</a></td></tr>'
        if cta_url
        else ""
    )
    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background-color:#F8FAFC;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#F8FAFC;">
    <tr><td align="center" style="padding:32px 16px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background-color:#FFFFFF;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        <tr><td style="background-color:#6C5CE7;padding:24px 28px;"><div style="font-size:22px;font-weight:700;color:#FFFFFF;">HTM</div></td></tr>
        <tr>
          <td style="padding:28px 24px 8px;">
            <div style="font-size:20px;font-weight:700;color:#1E293B;margin-bottom:6px;">New reply on your {escape(context_label.lower())}</div>
            <div style="font-size:14px;color:#64748B;line-height:1.6;">Hi {escape(recipient_name)},<br><strong>{escape(author_name)}</strong> left a reply on:</div>
            <div style="margin-top:10px;font-size:15px;font-weight:600;color:#1E293B;">{escape(context_label)} · <span style="color:#6C5CE7;">{escape(context_subtitle)}</span></div>
            {excerpt_html}
          </td>
        </tr>
        {cta_html}
        <tr><td style="padding:20px 24px;background-color:#F8FAFC;border-top:1px solid #E2E8F0;"><div style="font-size:13px;color:#94A3B8;text-align:center;">Automated alert from HTM</div></td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    return subject, html


def build_password_reset_code_email(code: str) -> tuple[str, str]:
    """Build password reset verification code email."""
    subject = "[Password] Reset code"
    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background-color:#F8FAFC;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#F8FAFC;">
    <tr><td align="center" style="padding:32px 16px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background-color:#FFFFFF;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        <tr><td style="background-color:#FF6B6B;padding:24px 28px;"><div style="font-size:22px;font-weight:700;color:#FFFFFF;">HTM</div></td></tr>
        <tr><td style="padding:32px 24px;text-align:center;">
          <div style="font-size:20px;font-weight:700;color:#1E293B;margin-bottom:8px;">Password Reset Code</div>
          <div style="font-size:14px;color:#64748B;margin-bottom:28px;">Enter this code to reset your password.</div>
          <div style="display:inline-block;padding:16px 40px;background-color:#F1F5F9;border-radius:8px;font-size:32px;font-weight:800;letter-spacing:8px;color:#1E293B;">{escape(code)}</div>
          <div style="font-size:13px;color:#94A3B8;margin-top:24px;">This code expires in 5 minutes.</div>
        </td></tr>
        <tr><td style="padding:20px 24px;background-color:#F8FAFC;border-top:1px solid #E2E8F0;"><div style="font-size:13px;color:#94A3B8;text-align:center;">Automated alert from HTM</div></td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    return subject, html


def build_temporary_password_email(temp_password: str) -> tuple[str, str]:
    """Build temporary password alert email."""
    subject = "[Password] Reset complete"
    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background-color:#F8FAFC;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#F8FAFC;">
    <tr><td align="center" style="padding:32px 16px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background-color:#FFFFFF;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        <tr><td style="background-color:#FDCB6E;padding:24px 28px;"><div style="font-size:22px;font-weight:700;color:#1E293B;">HTM</div></td></tr>
        <tr><td style="padding:32px 24px;text-align:center;">
          <div style="font-size:20px;font-weight:700;color:#1E293B;margin-bottom:8px;">Password Reset by Administrator</div>
          <div style="font-size:14px;color:#64748B;margin-bottom:28px;">Your password has been reset. Use the temporary password below to log in.</div>
          <div style="display:inline-block;padding:16px 40px;background-color:#FFF8E1;border:2px solid #FDCB6E;border-radius:8px;font-size:28px;font-weight:800;letter-spacing:4px;color:#1E293B;">{escape(temp_password)}</div>
          <div style="font-size:14px;color:#64748B;margin-top:24px;font-weight:600;">We recommend changing your password after logging in.</div>
        </td></tr>
        <tr><td style="padding:20px 24px;background-color:#F8FAFC;border-top:1px solid #E2E8F0;"><div style="font-size:13px;color:#94A3B8;text-align:center;">Automated alert from HTM</div></td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    return subject, html


# ---------------------------------------------------------------------------
# Schedule daily report
# ---------------------------------------------------------------------------

# 카테고리 정렬 우선순위 + 짧은 라벨 (한눈에 파악용)
_CATEGORY_ORDER = {
    "shift_understaffed": 0,
    "sv_missing": 1,
    "over_6h": 2,
    "no_break_8h": 3,
}
_CATEGORY_SHORT = {
    "shift_understaffed": "0 staff",
    "sv_missing": "No SV",
    "over_6h": "Over 6h",
    "no_break_8h": "No break / 8h+",
}
_CATEGORY_DOT = {
    "shift_understaffed": "#DC2626",  # red
    "sv_missing": "#EA580C",  # orange
    "over_6h": "#CA8A04",  # amber
    "no_break_8h": "#9333EA",  # purple
}
_STATUS_CHIP = {
    "NEW":      ("#FEE2E2", "#B91C1C"),
    "ONGOING":  ("#FEF3C7", "#92400E"),
    "RESOLVED": ("#D1FAE5", "#047857"),
}


def _issue_link(issue: "Issue", admin_base_url: str) -> str:
    """이슈 → 콘솔 daily view deeplink.

    Query params (console 의 usePersistedFilters / SchedulesCalendarView 기준):
      view=daily         : daily view 강제
      day=YYYY-MM-DD     : 선택 날짜
      stores=<uuid>      : 매장 필터 (consoleFiltersSync 가 인식하는 새 키)
      _ext=1             : 외부 진입 마커 — 본인 저장 필터 덮어쓰기 방지 (EXT_MARKER)
    """
    qs: dict[str, str] = {"view": "daily", "_ext": "1"}
    if issue.target_date:
        qs["day"] = issue.target_date
    if issue.store_id:
        qs["stores"] = issue.store_id
    return f"{admin_base_url.rstrip('/')}/schedules?{urlencode(qs)}"


def _daily_view_link(store_id: str | None, target_date: str, admin_base_url: str) -> str:
    """매장 + 날짜 단위 daily view deeplink (SV gap 섹션 날짜 헤더용)."""
    qs: dict[str, str] = {"view": "daily", "day": target_date, "_ext": "1"}
    if store_id:
        qs["stores"] = store_id
    return f"{admin_base_url.rstrip('/')}/schedules?{urlencode(qs)}"


def _weekly_view_link(store_id: str | None, week_anchor: date, admin_base_url: str) -> str:
    """매장 weekly view deeplink (Section 1 매트릭스 매장 헤더용)."""
    qs: dict[str, str] = {"view": "weekly", "week": week_anchor.isoformat(), "_ext": "1"}
    if store_id:
        qs["stores"] = store_id
    return f"{admin_base_url.rstrip('/')}/schedules?{urlencode(qs)}"


def _kpi_card(value: int, label: str, sublabel: str, color: str, bg: str) -> str:
    return f"""
            <td width="33%" valign="top" style="padding:0 6px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{bg};border-radius:8px;">
                <tr><td style="padding:22px 14px;text-align:center;">
                  <div style="font-size:14px;font-weight:800;letter-spacing:1.5px;color:{color};">{escape(label)}</div>
                  <div style="font-size:44px;font-weight:800;color:{color};line-height:1.1;margin-top:8px;">{value}</div>
                  <div style="font-size:13px;color:#0F172A;font-weight:600;margin-top:6px;">{escape(sublabel)}</div>
                </td></tr>
              </table>
            </td>"""


def _section_header(num: int, title: str) -> str:
    """공문서 톤 — 흰 배경 + 굵은 검정 underline. 컬러 없음."""
    return f"""
        <tr>
          <td style="padding:36px 32px 0;">
            <div style="font-size:12px;font-weight:700;color:#475569;letter-spacing:2px;margin-bottom:4px;">SECTION {num}</div>
            <div style="font-size:22px;font-weight:800;color:#0F172A;padding-bottom:10px;border-bottom:3px solid #0F172A;">{escape(title)}</div>
          </td>
        </tr>"""


def _staffing_matrix_for_store(
    store_name: str,
    store_cells: list["ShiftCell"],
    target_dates: list[date],
    store_id: str | None = None,
    admin_base_url: str | None = None,
) -> str:
    """매장 1개의 staffing matrix — 행: shift, 열: 날짜, 셀: staff count (0이면 빨강)."""
    if not store_cells:
        return f"""
        <tr>
          <td style="padding:16px 32px 4px;">
            <div style="font-size:17px;font-weight:800;color:#0F172A;border-bottom:1px solid #CBD5E1;padding-bottom:6px;">{escape(store_name)}</div>
            <div style="font-size:14px;color:#475569;font-weight:600;margin-top:6px;">No shifts configured</div>
          </td>
        </tr>"""

    # shift unique (sort_order 순)
    shift_meta: dict[str, tuple[str, int]] = {}  # shift_id → (name, sort_order)
    by_cell: dict[tuple[str, str], "ShiftCell"] = {}  # (shift_id, date_iso) → cell
    for c in store_cells:
        shift_meta[c.shift_id] = (c.shift_name, c.shift_sort_order)
        by_cell[(c.shift_id, c.target_date.isoformat())] = c
    sorted_shifts = sorted(shift_meta.items(), key=lambda kv: (kv[1][1], kv[1][0].lower()))

    # Header row: Shift | date1 | date2 | ...
    header_cells = '<th style="padding:12px 12px;text-align:left;font-size:15px;font-weight:800;color:#0F172A;border-bottom:2px solid #0F172A;">Shift</th>'
    for d in target_dates:
        dow = _DOW_LABEL[d.weekday()]
        header_cells += (
            f'<th style="padding:12px 12px;text-align:center;font-size:15px;font-weight:800;'
            f'color:#0F172A;border-bottom:2px solid #0F172A;">{d.strftime("%m/%d")} {dow}</th>'
        )

    # Body rows
    body_rows = ""
    for shift_id, (shift_name, _) in sorted_shifts:
        row = f'<td style="padding:12px 12px;font-size:16px;color:#0F172A;border-bottom:1px solid #CBD5E1;font-weight:700;">{escape(shift_name)}</td>'
        for d in target_dates:
            cell = by_cell.get((shift_id, d.isoformat()))
            if cell is None:
                val_html = '<span style="font-size:18px;color:#475569;font-weight:700;">—</span>'
            else:
                staff_color = "#B91C1C" if cell.staff_count == 0 else "#0F172A"
                sv_color = "#B91C1C" if cell.sv_count == 0 else "#0F172A"
                sv_weight = "800" if cell.sv_count == 0 else "600"
                val_html = (
                    f'<div style="font-size:20px;font-weight:800;color:{staff_color};">{cell.staff_count}</div>'
                    f'<div style="font-size:13px;font-weight:{sv_weight};color:{sv_color};margin-top:4px;">{cell.sv_count} SV</div>'
                )
            row += f'<td style="padding:12px 12px;text-align:center;border-bottom:1px solid #CBD5E1;">{val_html}</td>'
        body_rows += f"<tr>{row}</tr>"

    view_html = ""
    if admin_base_url and target_dates:
        wl = _weekly_view_link(store_id, target_dates[0], admin_base_url)
        view_html = f'<a href="{escape(wl)}" class="view-link">View week →</a>'

    return f"""
        <tr>
          <td style="padding:18px 32px 6px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-bottom:1px solid #CBD5E1;margin-bottom:6px;">
              <tr>
                <td style="padding-bottom:6px;font-size:17px;font-weight:800;color:#0F172A;">{escape(store_name)}</td>
                <td align="right" style="padding-bottom:6px;white-space:nowrap;">{view_html}</td>
              </tr>
            </table>
            <div style="font-size:13px;color:#475569;margin-bottom:8px;">Each cell: staff count + SV count (red if 0)</div>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #CBD5E1;">
              <thead><tr style="background:#F1F5F9;">{header_cells}</tr></thead>
              <tbody>{body_rows}</tbody>
            </table>
          </td>
        </tr>"""


def _user_issue_row(issue: "Issue", status: str, admin_base_url: str) -> str:
    """user 단위 이슈 (OT / no break) 한 줄 + 변경 마커. row 자체가 hover 대상."""
    link = _issue_link(issue, admin_base_url)
    hours_txt = ""
    if issue.detail and "total_minutes" in issue.detail:
        hours_txt = f' — <b style="color:#B91C1C;font-size:18px;">{issue.detail["total_minutes"] / 60:.1f}h</b>'
    store_part = f' <span style="color:#0F172A;font-weight:600;">({escape(issue.store_name)})</span>' if issue.store_name else ""
    marker = _change_marker(status)
    row_style = _resolved_style() if status == "RESOLVED" else "color:#0F172A;"
    return f"""
        <tr class="row-block">
          <td valign="middle" style="padding:10px 12px;font-size:17px;{row_style}">
            {marker}<b>{escape(issue.user_name or "—")}</b>{store_part}{hours_txt}
          </td>
          <td valign="middle" align="right" style="padding:10px 12px;white-space:nowrap;">
            <a href="{escape(link)}" class="view-link">View →</a>
          </td>
        </tr>"""


def _fmt_duration(mins: int) -> str:
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def _fmt_time_label(mins: int) -> str:
    # 자정 넘어 1440 이상은 +1440 처리되어 있으므로 mod 24
    h = (mins // 60) % 24
    m = mins % 60
    return f"{h:02d}:{m:02d}"


def _section_sv_gaps(
    num: int,
    title: str,
    sv_issues: list[tuple["Issue", str]],
    stores: list["StoreInfo"],
    target_dates: list[date],
    admin_base_url: str,
) -> str:
    """SV gap 섹션 — 매장 → 날짜 → 시간 구간 list (operating_hours 기반).

    매장이 covered 여도 각 날짜는 한 줄씩 표시. View 링크는 날짜 헤더 옆 1개만
    (daily view 로 이동).
    """
    header = _section_header(num, title)
    if not stores:
        return header + """
        <tr><td style="padding:4px 28px 16px;font-size:14px;color:#64748B;">
          No active stores.
        </td></tr>"""

    # store_id → date_iso → list of (issue, status)
    by_store_date: dict[str, dict[str, list[tuple["Issue", str]]]] = {}
    for i, status in sv_issues:
        if i.store_id and i.target_date:
            by_store_date.setdefault(i.store_id, {}).setdefault(i.target_date, []).append((i, status))

    blocks_html = ""
    for st in stores:
        store_gaps = by_store_date.get(st.id, {})
        # active(NEW/ONGOING) 만 카운트 — RESOLVED 는 "해결됨" 정보 표시용이지 매장 상태는 아님
        active_count = sum(1 for v in store_gaps.values() for _, s in v if s != "RESOLVED")
        resolved_count = sum(1 for v in store_gaps.values() for _, s in v if s == "RESOLVED")
        if active_count == 0:
            badge = '<span style="font-size:16px;font-weight:800;color:#047857;">SV covered across operating hours</span>'
        else:
            badge = f'<span style="font-size:16px;font-weight:800;color:#B91C1C;">{active_count} gap(s)</span>'
        if resolved_count:
            badge += f' <span style="font-size:14px;font-weight:700;color:#047857;margin-left:8px;">· {resolved_count} resolved since last</span>'

        store_header = f"""
        <tr><td style="padding:20px 32px 4px;">
          <div style="font-size:17px;font-weight:800;color:#0F172A;border-bottom:1px solid #CBD5E1;padding-bottom:6px;">{escape(st.name)}</div>
          <div style="margin-top:6px;">{badge}</div>
        </td></tr>"""

        # 매장이 covered 든 아니든 모든 날짜를 한 줄씩 표시
        date_blocks = ""
        for d in target_dates:
            d_iso = d.isoformat()
            dow = _DOW_LABEL[d.weekday()]
            day_gaps = store_gaps.get(d_iso, [])
            day_gaps_sorted = sorted(day_gaps, key=lambda x: (x[0].detail or {}).get("start_minute", 0))
            day_active = sum(1 for _, s in day_gaps_sorted if s != "RESOLVED")
            view_link = _daily_view_link(st.id, d_iso, admin_base_url)
            view_html = f'<a href="{escape(view_link)}" class="view-link">View →</a>'

            if not day_gaps_sorted:
                date_blocks += f"""
        <tr><td style="padding:6px 32px;">
          <table class="row-block" role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F8FAFC;">
            <tr>
              <td style="padding:10px 14px;font-size:15px;font-weight:700;color:#0F172A;">
                {escape(d_iso)} {escape(dow)}:
                <span style="color:#047857;font-weight:700;margin-left:8px;">covered</span>
              </td>
              <td align="right" style="padding:10px 14px;white-space:nowrap;">{view_html}</td>
            </tr>
          </table>
        </td></tr>"""
            else:
                gap_rows = ""
                # 운영시간 표시 (첫 active issue detail 에서)
                first_for_window = next((i for i, s in day_gaps_sorted if s != "RESOLVED"), day_gaps_sorted[0][0])
                window_open = (first_for_window.detail or {}).get("window_open")
                window_close = (first_for_window.detail or {}).get("window_close")
                window_label = ""
                if window_open is not None and window_close is not None:
                    window_label = f' <span style="font-size:14px;color:#475569;font-weight:700;">(operating {_fmt_time_label(window_open)}–{_fmt_time_label(window_close)})</span>'

                for i, status in day_gaps_sorted:
                    det = i.detail or {}
                    s_m = det.get("start_minute", 0)
                    e_m = det.get("end_minute", 0)
                    dur_m = det.get("duration_minutes", e_m - s_m)
                    marker = _change_marker(status)
                    row_style = _resolved_style() if status == "RESOLVED" else "color:#0F172A;"
                    time_color = "#475569" if status == "RESOLVED" else "#B91C1C"
                    gap_rows += f"""
          <tr>
            <td style="padding:8px 0;font-size:17px;{row_style}">
              {marker}<b style="color:{time_color};font-size:18px;">{_fmt_time_label(s_m)}–{_fmt_time_label(e_m)}</b>
              <span style="color:#0F172A;margin-left:10px;font-weight:600;">{_fmt_duration(dur_m)} without SV</span>
            </td>
          </tr>"""
                # 헤더의 카운트 표시: active만 (RESOLVED는 옆에 별도)
                day_resolved = len(day_gaps_sorted) - day_active
                if day_active and day_resolved:
                    head_cnt = f'<span style="color:#B91C1C;font-weight:800;">— {day_active} gap(s)</span> <span style="color:#047857;font-weight:700;font-size:14px;">+ {day_resolved} resolved</span>'
                elif day_active:
                    head_cnt = f'<span style="color:#B91C1C;font-weight:800;">— {day_active} gap(s)</span>'
                else:
                    head_cnt = f'<span style="color:#047857;font-weight:800;">— {day_resolved} resolved since last</span>'
                date_blocks += f"""
        <tr><td style="padding:6px 32px;">
          <table class="row-block" role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F1F5F9;">
            <tr><td style="padding:12px 14px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="font-size:15px;font-weight:800;color:#0F172A;">
                    {escape(d_iso)} {escape(dow)} {head_cnt}{window_label}
                  </td>
                  <td align="right" style="white-space:nowrap;">{view_html}</td>
                </tr>
              </table>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;">{gap_rows}</table>
            </td></tr>
          </table>
        </td></tr>"""

        blocks_html += store_header + date_blocks

    return header + blocks_html


def _by_date_block(
    sub_title: str,
    sub_caption: str,
    issues: list[tuple["Issue", str]],
    dates_to_show: list[date],
    admin_base_url: str,
) -> str:
    """sub-group (Planned / Actual) — 각 날짜별 row + 변경 마커."""
    by_date: dict[str, list[tuple["Issue", str]]] = {}
    for i, s in issues:
        by_date.setdefault(i.target_date, []).append((i, s))

    date_rows = ""
    for d in dates_to_show:
        d_iso = d.isoformat()
        dow = _DOW_LABEL[d.weekday()]
        day = by_date.get(d_iso, [])
        if not day:
            date_rows += f"""
        <tr><td style="padding:6px 32px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F8FAFC;">
            <tr><td style="padding:10px 14px;">
              <span style="font-size:15px;font-weight:700;color:#0F172A;">{escape(d_iso)} {escape(dow)}:</span>
              <span style="font-size:15px;color:#047857;font-weight:700;margin-left:10px;">No issues</span>
            </td></tr>
          </table>
        </td></tr>"""
        else:
            day_sorted = sorted(day, key=lambda x: (x[0].user_name or "").lower())
            user_rows = "".join(_user_issue_row(i, s, admin_base_url) for i, s in day_sorted)
            active_n = sum(1 for _, s in day_sorted if s != "RESOLVED")
            resolved_n = len(day_sorted) - active_n
            if active_n and resolved_n:
                head_cnt = f'<span style="color:#B91C1C;font-weight:800;">— {active_n}</span> <span style="color:#047857;font-weight:700;font-size:14px;">+ {resolved_n} resolved</span>'
            elif active_n:
                head_cnt = f'<span style="color:#B91C1C;font-weight:800;">— {active_n}</span>'
            else:
                head_cnt = f'<span style="color:#047857;font-weight:800;">— {resolved_n} resolved since last</span>'
            date_rows += f"""
        <tr><td style="padding:6px 32px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F1F5F9;">
            <tr><td style="padding:10px 14px;">
              <div style="font-size:15px;font-weight:800;color:#0F172A;">
                {escape(d_iso)} {escape(dow)} {head_cnt}
              </div>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;">{user_rows}</table>
            </td></tr>
          </table>
        </td></tr>"""

    return f"""
        <tr><td style="padding:18px 32px 4px;">
          <div style="font-size:16px;font-weight:800;color:#0F172A;border-bottom:1px solid #CBD5E1;padding-bottom:6px;">{escape(sub_title)}</div>
          <div style="font-size:13px;color:#475569;margin-top:4px;">{escape(sub_caption)}</div>
        </td></tr>
        {date_rows}"""


def _section_user_issues_two_sources(
    num: int,
    title: str,
    planned_issues: list[tuple["Issue", str]],
    actual_issues: list[tuple["Issue", str]],
    planned_dates: list[date],
    actual_dates: list[date],
    planned_caption: str,
    actual_caption: str,
    admin_base_url: str,
) -> str:
    """OT / No break 섹션 — Planned + Actual 각각 날짜별 row."""
    return (
        _section_header(num, title)
        + _by_date_block("Planned (schedule)", planned_caption, planned_issues, planned_dates, admin_base_url)
        + _by_date_block("Actual (attendance)", actual_caption, actual_issues, actual_dates, admin_base_url)
    )


def _collect_by_category(diff: "ReportDiff", category: str) -> list[tuple["Issue", str]]:
    """카테고리별 이슈 + status (NEW/ONGOING/RESOLVED) 통합 — Planned 섹션용.

    Planned 는 N일치(예: 3일)가 매일 겹쳐서 발송되므로 diff 비교가 의미 있다.
    """
    out: list[tuple["Issue", str]] = []
    for status, items in (("NEW", diff.new), ("ONGOING", diff.ongoing), ("RESOLVED", diff.resolved)):
        for i in items:
            if i.category == category:
                out.append((i, status))
    return out


def _collect_actual(diff: "ReportDiff", category: str) -> list[tuple["Issue", str]]:
    """Actual (yesterday attendance) 섹션용 — 어제 1일치만 보여주므로 diff 의미 없음.

    매일 yesterday 가 새 날짜라 같은 이슈가 두 번 보고에 나타날 일이 없음 → NEW/RESOLVED 마커 무의미.
    RESOLVED 는 어차피 발생하지 않지만 방어적으로 제외하고, 모두 status="ONGOING" 으로 처리하여 마커 표시 안 함.
    """
    out: list[tuple["Issue", str]] = []
    for items in (diff.new, diff.ongoing):  # resolved 제외
        for i in items:
            if i.category == category:
                out.append((i, "ONGOING"))  # 강제 ONGOING → _change_marker 빈 문자열
    return out


def _change_marker(status: str) -> str:
    """변경 마커 — NEW/RESOLVED 만 표시 (단색 outline 톤)."""
    if status == "NEW":
        return '<span style="display:inline-block;padding:1px 7px;border:1px solid #B91C1C;border-radius:2px;font-size:11px;font-weight:800;color:#B91C1C;margin-right:8px;letter-spacing:0.8px;">NEW</span>'
    if status == "RESOLVED":
        return '<span style="display:inline-block;padding:1px 7px;border:1px solid #047857;border-radius:2px;font-size:11px;font-weight:800;color:#047857;margin-right:8px;letter-spacing:0.8px;">RESOLVED</span>'
    return ""


def _resolved_style() -> str:
    return "color:#475569;text-decoration:line-through;"


def build_schedule_daily_report_email(
    *,
    org_name: str,
    sent_date: date,
    target_dates: list[date],
    yesterday: date | None = None,
    diff: "ReportDiff",
    stores: list["StoreInfo"],
    cells: list["ShiftCell"],
    admin_base_url: str,
) -> tuple[str, str]:
    """공문서 톤 — 큰 섹션 H2 + 매트릭스/list. 디자인 최소화."""
    period_label = f"{target_dates[0].isoformat()} ~ {target_dates[-1].isoformat()}"
    subject = f"[Schedule Report] {sent_date.isoformat()} ({org_name})"
    planned_caption = f"Schedule for {period_label} ({len(target_dates)} days)"
    actual_caption = f"{yesterday.isoformat()} attendance — final state (corrections reflected)" if yesterday else "Previous day attendance — final state"

    # ─── Summary ────────────────────────────────────────────────
    all_issues = list(diff.new) + list(diff.ongoing) + list(diff.resolved)
    sv_gap_n = sum(1 for i in all_issues if i.category == "sv_gap")
    ot_planned_n = sum(1 for i in all_issues if i.category == "over_6h")
    ot_actual_n = sum(1 for i in all_issues if i.category == "att_over_6h")
    nb_planned_n = sum(1 for i in all_issues if i.category == "no_break_8h")
    nb_actual_n = sum(1 for i in all_issues if i.category == "att_no_break_8h")
    understaffed_n = sum(1 for i in all_issues if i.category == "shift_understaffed")

    def _stat(n: int) -> str:
        color = "#B91C1C" if n else "#0F172A"
        return f'<b style="color:{color};font-size:20px;">{n}</b>'

    summary_html = f"""
        <tr>
          <td style="padding:22px 32px 20px;background:#F8FAFC;border-top:1px solid #CBD5E1;border-bottom:1px solid #CBD5E1;">
            <div style="font-size:12px;font-weight:700;color:#475569;letter-spacing:2px;margin-bottom:8px;">SUMMARY</div>
            <div style="font-size:15px;color:#0F172A;line-height:1.8;">
              Period <b>{escape(period_label)}</b> ({len(target_dates)} days) &nbsp;·&nbsp; Stores <b>{len(stores)}</b>
            </div>
            <div style="font-size:15px;color:#0F172A;line-height:1.9;margin-top:6px;">
              Understaffed shifts {_stat(understaffed_n)}
              &nbsp;·&nbsp; SV gaps {_stat(sv_gap_n)}
              &nbsp;·&nbsp; Overtime (planned/actual) {_stat(ot_planned_n)}<span style="color:#475569;">/</span>{_stat(ot_actual_n)}
              &nbsp;·&nbsp; No break 8h+ (planned/actual) {_stat(nb_planned_n)}<span style="color:#475569;">/</span>{_stat(nb_actual_n)}
            </div>
          </td>
        </tr>"""

    # ─── Section 1: Staffing by Shift ───────────────────────────
    cells_by_store: dict[str, list["ShiftCell"]] = {}
    for c in cells:
        cells_by_store.setdefault(c.store_id, []).append(c)

    # service 에서 이미 Store.created_at 순으로 정렬되어 옴 → 그 순서 유지
    sorted_stores = list(stores)
    matrices_html = "".join(
        _staffing_matrix_for_store(s.name, cells_by_store.get(s.id, []), target_dates, store_id=s.id, admin_base_url=admin_base_url)
        for s in sorted_stores
    )
    sec1 = (
        _section_header(1, "Staffing by Shift")
        + f"""
        <tr><td style="padding:4px 32px 8px;font-size:14px;color:#0F172A;font-weight:600;">
          Staff count per shift × day. <span style="color:#B91C1C;font-weight:800;">0</span> = no one scheduled. <span style="color:#475569;font-weight:800;">—</span> = outside operating hours.
        </td></tr>"""
        + matrices_html
    )

    # ─── Section 2: Supervisor Coverage — 시간 기반 SV gap (매장→날짜→구간) ─
    sec2 = _section_sv_gaps(
        2,
        "Supervisor Coverage",
        _collect_by_category(diff, "sv_gap"),
        stores=sorted_stores,
        target_dates=target_dates,
        admin_base_url=admin_base_url,
    )

    actual_dates = [yesterday] if yesterday else []

    # ─── Section 3: Overtime (6h+) — Planned + Actual ───────────
    sec3 = _section_user_issues_two_sources(
        3,
        "Overtime — Work over 6 hours",
        planned_issues=_collect_by_category(diff, "over_6h"),
        actual_issues=_collect_actual(diff, "att_over_6h"),
        planned_dates=target_dates,
        actual_dates=actual_dates,
        planned_caption=planned_caption,
        actual_caption=actual_caption,
        admin_base_url=admin_base_url,
    )

    # ─── Section 4: No Break with 8h+ — Planned + Actual ────────
    sec4 = _section_user_issues_two_sources(
        4,
        "No Break with 8h or more",
        planned_issues=_collect_by_category(diff, "no_break_8h"),
        actual_issues=_collect_actual(diff, "att_no_break_8h"),
        planned_dates=target_dates,
        actual_dates=actual_dates,
        planned_caption=planned_caption,
        actual_caption=actual_caption,
        admin_base_url=admin_base_url,
    )

    schedule_link = f"{admin_base_url.rstrip('/')}/schedules"

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
  /* Row hover — 마우스 올린 행 / block 의 배경이 옅은 파랑으로 강조, View 버튼은 파랑으로 채워짐 */
  .row-hover, .row-block {{ transition: background 0.12s; }}
  .row-hover:hover, .row-hover:hover .row-block, .row-block:hover {{ background: #EFF6FF !important; }}
  .row-hover:hover .view-link, .row-block:hover .view-link {{ background: #2563EB; color: #FFFFFF !important; border-color:#2563EB !important; }}
  .view-link {{ display:inline-block; padding:5px 12px; border:1px solid #2563EB; border-radius:4px; font-size:14px; font-weight:700; color:#2563EB; text-decoration:none; transition: background 0.12s, color 0.12s; }}
  .view-link:hover {{ background:#2563EB; color:#FFFFFF !important; }}
</style>
</head>
<body style="margin:0;padding:0;background-color:#F1F5F9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0F172A;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#F1F5F9;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="760" cellpadding="0" cellspacing="0" style="max-width:760px;width:100%;background-color:#FFFFFF;border:1px solid #CBD5E1;">
        <tr>
          <td style="padding:28px 32px 20px;border-bottom:3px solid #0F172A;">
            <div style="font-size:13px;color:#475569;letter-spacing:1.8px;font-weight:800;">HTM · DAILY SCHEDULE REPORT</div>
            <div style="font-size:26px;font-weight:800;color:#0F172A;margin-top:8px;">{escape(org_name)}</div>
            <div style="font-size:15px;color:#0F172A;margin-top:6px;">Sent: <b>{escape(sent_date.isoformat())}</b></div>
          </td>
        </tr>
        {summary_html}
        {sec1}
        {sec2}
        {sec3}
        {sec4}
        <tr>
          <td style="padding:28px 32px;border-top:1px solid #CBD5E1;">
            <a href="{escape(schedule_link)}" style="display:inline-block;padding:14px 24px;background:#0F172A;color:#FFFFFF;text-decoration:none;border-radius:6px;font-size:16px;font-weight:800;">Open Schedule Console →</a>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 32px;background:#F8FAFC;border-top:1px solid #CBD5E1;">
            <div style="font-size:13px;color:#0F172A;font-weight:600;">Automated daily report · HTM</div>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    return subject, html
