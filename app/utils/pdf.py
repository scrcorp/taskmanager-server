"""PDF generation utilities."""

from pathlib import Path

from fpdf import FPDF

# 유니코드 지원 폰트 — macOS 시스템 폰트, 없으면 Helvetica fallback
_FONT_PATH = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")


def create_pdf() -> tuple[FPDF, str]:
    """Create a new FPDF instance with unicode font configured.

    Returns:
        (pdf, font_name) tuple
    """
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)

    if _FONT_PATH.exists():
        pdf.add_font("main", "", str(_FONT_PATH))
        pdf.add_font("main", "B", str(_FONT_PATH))
        font = "main"
    else:
        font = "Helvetica"

    return pdf, font


def build_daily_report_pdf(
    store_name: str,
    report_date: str,
    period: str,
    author_name: str,
    submitted_at: str,
    sections: list[dict],
) -> tuple[str, bytes]:
    """Build daily report PDF.

    Returns:
        (filename, pdf_bytes) tuple
    """
    period_label = "Lunch" if period == "lunch" else "Dinner"
    date_compact = report_date.replace("-", "")
    filename = f"DailyReport_{store_name.replace(' ', '')}_{date_compact}_{period_label}.pdf"

    pdf, font = create_pdf()
    pdf.add_page()

    # Header bar
    pdf.set_fill_color(37, 99, 235)  # #2563EB
    pdf.rect(0, 0, 210, 18, "F")
    pdf.set_font(font, "B", 16)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(14, 3)
    pdf.cell(0, 12, "TaskManager — Daily Report", new_x="LMARGIN", new_y="NEXT")

    # Meta section
    pdf.ln(8)
    meta = [
        ("Store", store_name),
        ("Date", report_date),
        ("Period", period_label),
        ("Author", author_name),
        ("Submitted", submitted_at),
    ]
    for label, value in meta:
        pdf.set_font(font, "B", 11)
        pdf.set_text_color(100, 116, 139)  # #64748B
        pdf.cell(30, 7, label, new_x="RIGHT")
        pdf.set_font(font, "", 11)
        pdf.set_text_color(30, 41, 59)  # #1E293B
        pdf.cell(0, 7, value, new_x="LMARGIN", new_y="NEXT")

    # Divider
    pdf.ln(4)
    pdf.set_draw_color(226, 232, 240)  # #E2E8F0
    pdf.line(14, pdf.get_y(), 196, pdf.get_y())
    pdf.ln(6)

    # Sections
    for s in sections:
        title = s.get("title", "")
        content = (s.get("content") or "").strip()
        if not content:
            content = "No content"

        pdf.set_font(font, "B", 12)
        pdf.set_text_color(37, 99, 235)  # #2563EB
        pdf.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")

        pdf.set_font(font, "", 11)
        pdf.set_text_color(51, 65, 85)  # #334155
        pdf.multi_cell(0, 6, content, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

    # Footer
    pdf.ln(4)
    pdf.set_draw_color(226, 232, 240)
    pdf.line(14, pdf.get_y(), 196, pdf.get_y())
    pdf.ln(4)
    pdf.set_font(font, "", 9)
    pdf.set_text_color(148, 163, 184)  # #94A3B8
    pdf.cell(0, 6, "Automated report from TaskManager", align="C")

    return filename, pdf.output()
