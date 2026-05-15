"""IRS Form 4070 — Employee's Report of Tips to Employer.

본 generator 는 fpdf2 로 양식의 핵심 정보(4 boxes + 식별/서명 영역)를 한 페이지에 담는다.
완벽한 IRS Rev 8-2005 fidelity 는 별도 작업 — 본 구현은 페이롤 record 와 회계사 검토용
"identifiable, signable, archivable" 수준.

가이드 §8.6 결정사항:
    Box 1: Cash tips received        — 본인 cash kept 합
    Box 2: Credit-card tips received — 본인 카드 + 분배받은 카드 (accepted only)
    Box 3: Tips paid out             — 본인이 분배한 카드 합
    Box 4: Net tips reported         — Box1 + Box2 − Box3

가이드 §1.11: 페이지 2 (Purpose + Paperwork Reduction Act Notice) — 본 구현은 page 1 만.
나중에 page 2 추가 시 IRS PDF 의 instructions 텍스트를 그대로 옮긴다.
"""

from io import BytesIO
from typing import Optional

from app.utils.pdf import create_pdf


def build_form_4070_pdf(
    *,
    employee_name: str,
    employee_email: Optional[str],
    period_start: str,
    period_end: str,
    store_name: str,
    cash_tips: str,
    card_tips: str,
    paid_out: str,
    net_tips: str,
    signed_at: Optional[str] = None,
    signature_png: Optional[bytes] = None,
) -> bytes:
    """Form 4070 PDF bytes 반환."""
    pdf, font = create_pdf()
    pdf.add_page()

    # ── 헤더 밴드 ────────────────────────────────────────────
    pdf.set_fill_color(108, 92, 231)  # #6C5CE7
    pdf.rect(0, 0, 210, 22, "F")
    pdf.set_font(font, "B", 16)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(14, 5)
    pdf.cell(0, 6, "Form 4070", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font, "", 10)
    pdf.set_x(14)
    pdf.cell(0, 5, "Employee's Report of Tips to Employer", new_x="LMARGIN", new_y="NEXT")

    pdf.set_text_color(0, 0, 0)
    pdf.set_y(30)

    # ── Employee identification ──────────────────────────────
    pdf.set_font(font, "B", 11)
    pdf.cell(0, 6, "Employee", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font, "", 10)
    pdf.cell(0, 5, f"Name: {employee_name}", new_x="LMARGIN", new_y="NEXT")
    if employee_email:
        pdf.cell(0, 5, f"Email: {employee_email}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # ── Establishment + Period ───────────────────────────────
    pdf.set_font(font, "B", 11)
    pdf.cell(0, 6, "Establishment & Period", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font, "", 10)
    pdf.cell(0, 5, f"Establishment: {store_name}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"Period: {period_start} – {period_end}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # ── 4 amount boxes ───────────────────────────────────────
    pdf.set_font(font, "B", 11)
    pdf.cell(0, 6, "Reported amounts (USD)", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    _amount_row(pdf, font, "1. Cash tips received", cash_tips)
    _amount_row(pdf, font, "2. Credit-card tips received", card_tips)
    _amount_row(pdf, font, "3. Tips paid out to other employees", paid_out)
    pdf.ln(2)
    _amount_row(pdf, font, "4. Net tips (1 + 2 − 3)", net_tips, bold=True, accent=True)
    pdf.ln(8)

    # ── Signature area ───────────────────────────────────────
    pdf.set_font(font, "B", 11)
    pdf.cell(0, 6, "Employee signature", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(180, 180, 180)
    pdf.rect(14, pdf.get_y(), 120, 30)
    sig_top = pdf.get_y() + 2
    if signature_png:
        # signature image (in-memory)
        try:
            pdf.image(BytesIO(signature_png), x=18, y=sig_top, w=110, h=24, keep_aspect_ratio=True)
        except Exception:
            pass
    pdf.set_y(pdf.get_y() + 32)
    pdf.set_font(font, "", 9)
    if signed_at:
        pdf.cell(0, 5, f"Date signed: {signed_at}", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_text_color(180, 60, 60)
        pdf.cell(0, 5, "AWAITING SIGNATURE", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    # ── Footer / legal note ──────────────────────────────────
    pdf.ln(8)
    pdf.set_font(font, "", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(
        0, 4,
        "This document records tips received and tips paid out for the period above. "
        "Under penalties of perjury, the employee declares this report is true, correct, "
        "and complete. Filed with the establishment for IRS reporting (Form 4070 series).",
    )

    # ── Page 2 — Purpose + Paperwork Reduction Act Notice (IRS Rev 8-2005) ─
    _write_page_2(pdf, font)

    # ── bytes 반환
    out = pdf.output(dest="S")
    return bytes(out)


def _write_page_2(pdf, font: str) -> None:
    """IRS Form 4070 페이지 2 — Purpose + Paperwork Reduction Act Notice.

    가이드 §1.9 / §8.6 결정: IRS PDF Rev 8-2005 페이지 2 텍스트 그대로 옮긴다.
    회계사·직원이 양식 의미를 확인할 수 있게 본 페이지를 동봉.
    """
    pdf.add_page()
    pdf.set_text_color(0, 0, 0)

    pdf.set_font(font, "B", 14)
    pdf.cell(0, 8, "Purpose", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_font(font, "", 10)
    pdf.multi_cell(
        0, 5,
        "Use this form to report tips you receive to your employer. This includes cash tips, "
        "tips you receive from other employees, and debit and credit card tips. You must report "
        "tips every month regardless of your total wages and tips for the year. However, you do "
        "not have to report tips to your employer for any month you received less than $20 in "
        "tips while working for that employer.",
    )
    pdf.ln(2)
    pdf.multi_cell(
        0, 5,
        "Report tips by the 10th day of the month following the month that you receive them. "
        "If the 10th day is a Saturday, Sunday, or legal holiday, report tips by the next day "
        "that is not a Saturday, Sunday, or legal holiday.",
    )
    pdf.ln(2)
    pdf.multi_cell(
        0, 5,
        "See Pub. 531, Reporting Tip Income, for more details.",
    )

    pdf.ln(6)
    pdf.set_font(font, "B", 14)
    pdf.cell(0, 8, "Paperwork Reduction Act Notice", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_font(font, "", 9)
    pdf.multi_cell(
        0, 4.5,
        "We ask for the information on this form to carry out the Internal Revenue laws of the "
        "United States. We need it to ensure that you are complying with these laws and to "
        "allow us to figure and collect the right amount of tax.",
    )
    pdf.ln(1)
    pdf.multi_cell(
        0, 4.5,
        "You are not required to provide the information requested on a form that is subject to "
        "the Paperwork Reduction Act unless the form displays a valid OMB control number. Books "
        "or records relating to a form or its instructions must be retained as long as their "
        "contents may become material in the administration of any Internal Revenue law. "
        "Generally, tax returns and return information are confidential, as required by Code "
        "section 6103.",
    )
    pdf.ln(1)
    pdf.multi_cell(
        0, 4.5,
        "The time needed to complete this form will vary depending on individual circumstances. "
        "The estimated average time is 7 minutes.",
    )
    pdf.ln(1)
    pdf.multi_cell(
        0, 4.5,
        "If you have comments concerning the accuracy of this time estimate or suggestions for "
        "making this form simpler, we would be happy to hear from you. You can write to the "
        "Internal Revenue Service, Tax Products Coordinating Committee, "
        "SE:W:CAR:MP:T:T:SP, 1111 Constitution Ave. NW, IR-6526, Washington, DC 20224. Do not "
        "send this form to this address. Instead, give it to your employer.",
    )

    pdf.ln(6)
    pdf.set_font(font, "", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 4, "Form 4070 (Rev. 8-2005) — reproduced from IRS public domain.", new_x="LMARGIN", new_y="NEXT")


def _amount_row(pdf, font: str, label: str, amount: str, *, bold: bool = False, accent: bool = False) -> None:
    pdf.set_font(font, "B" if bold else "", 11 if bold else 10)
    if accent:
        pdf.set_fill_color(245, 240, 255)
        pdf.set_text_color(108, 92, 231)
        pdf.cell(120, 8, label, fill=True)
        pdf.cell(70, 8, f"$ {amount}", align="R", fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
    else:
        pdf.cell(120, 7, label)
        pdf.cell(70, 7, f"$ {amount}", align="R", new_x="LMARGIN", new_y="NEXT")
