"""Email HTML template builder."""

from html import escape


def build_daily_report_email(
    store_name: str,
    report_date: str,
    period: str,
    author_name: str,
    submitted_at: str,
    sections: list[dict],
) -> tuple[str, str]:
    """Build daily report submission notification email.

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
            <div style="font-size:22px;font-weight:700;color:#FFFFFF;">TaskManager</div>
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
            <div style="font-size:13px;color:#94A3B8;text-align:center;">Automated notification from TaskManager</div>
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
    """Build checklist completion notification email.

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
            <div style="font-size:22px;font-weight:700;color:#FFFFFF;">TaskManager</div>
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
            <div style="font-size:13px;color:#94A3B8;text-align:center;">Automated notification from TaskManager</div>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    return subject, html
