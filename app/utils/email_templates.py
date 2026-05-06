"""Email HTML template builder."""

from html import escape


def build_verification_code_email(code: str) -> tuple[str, str]:
    """Build email verification code email.

    Returns:
        (subject, html_body) tuple
    """
    subject = "[HTM] Email Verification Code"

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
    subject = f"[HTM] {author_name} replied on your {context_label.lower()}"
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
    subject = "[HTM] Password Reset Code"
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
    subject = "[HTM] Your Password Has Been Reset"
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
