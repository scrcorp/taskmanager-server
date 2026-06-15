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

# 서명 박스 좌표 (mm) — strokes/이미지 모두 같은 rect 안에 그린다.
_SIG_BOX_X = 14.0
_SIG_BOX_W = 120.0
_SIG_BOX_H = 30.0
# 박스 내부 패딩 — stroke 가 테두리에 붙지 않게.
_SIG_PAD = 3.0


def _draw_signature_strokes(pdf, signature_strokes: dict, box_top: float) -> bool:
    """벡터 서명 stroke 를 서명 박스 안에 그린다 (정규화 0..1 → mm 매핑).

    signature_strokes = {"strokes": [[[x,y]..]..], "aspect": w/h}.
    aspect 를 유지하며 (xMidYMid meet) 박스 안쪽 패딩 영역에 맞춘다.
    유효한 stroke 를 하나라도 그렸으면 True, 아니면 False.
    """
    strokes = (signature_strokes or {}).get("strokes")
    if not isinstance(strokes, list) or not strokes:
        return False

    avail_w = _SIG_BOX_W - 2 * _SIG_PAD
    avail_h = _SIG_BOX_H - 2 * _SIG_PAD
    if avail_w <= 0 or avail_h <= 0:
        return False

    aspect = (signature_strokes or {}).get("aspect") or 1.0
    try:
        aspect = float(aspect)
    except (TypeError, ValueError):
        aspect = 1.0
    if aspect <= 0:
        aspect = 1.0

    # aspect 유지하며 박스 안에 맞추기 (meet).
    draw_w = avail_w
    draw_h = draw_w / aspect
    if draw_h > avail_h:
        draw_h = avail_h
        draw_w = draw_h * aspect
    origin_x = _SIG_BOX_X + _SIG_PAD + (avail_w - draw_w) / 2
    origin_y = box_top + _SIG_PAD + (avail_h - draw_h) / 2

    # 잉크 중앙배치 — stroke bbox 중심을 그리기 영역 중심으로 (크기 유지, translate).
    min_x = min_y = 1.0
    max_x = max_y = 0.0
    seen = False
    for stroke in strokes:
        if not isinstance(stroke, list):
            continue
        for pt in stroke:
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                continue
            try:
                bx, by = float(pt[0]), float(pt[1])
            except (TypeError, ValueError):
                continue
            bx = min(1.0, max(0.0, bx))
            by = min(1.0, max(0.0, by))
            min_x = min(min_x, bx)
            max_x = max(max_x, bx)
            min_y = min(min_y, by)
            max_y = max(max_y, by)
            seen = True
    ox = (0.5 - (min_x + max_x) / 2) if seen else 0.0
    oy = (0.5 - (min_y + max_y) / 2) if seen else 0.0

    pdf.set_draw_color(20, 20, 24)
    pdf.set_line_width(0.4)
    drew = False
    for stroke in strokes:
        if not isinstance(stroke, list) or not stroke:
            continue
        pts: list[tuple[float, float]] = []
        for pt in stroke:
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                continue
            x, y = pt
            try:
                fx, fy = float(x), float(y)
            except (TypeError, ValueError):
                continue
            # 0..1 clamp 후 박스 좌표로 매핑.
            fx = min(1.0, max(0.0, fx))
            fy = min(1.0, max(0.0, fy))
            pts.append((origin_x + (fx + ox) * draw_w, origin_y + (fy + oy) * draw_h))
        if not pts:
            continue
        if len(pts) == 1:
            # 점 하나 — 보이도록 아주 짧은 선.
            x0, y0 = pts[0]
            pdf.line(x0, y0, x0 + 0.3, y0)
            drew = True
            continue
        for i in range(len(pts) - 1):
            pdf.line(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
        drew = True
    # 기본 선폭 복원 (이후 rect/테두리 그리기 영향 방지).
    pdf.set_line_width(0.2)
    return drew


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
    signature_strokes: Optional[dict] = None,
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
    box_top = pdf.get_y()
    # 우선순위: 벡터 strokes → 레거시 이미지(이미 서명된 구 폼) fallback.
    drew_vector = False
    if signature_strokes:
        try:
            drew_vector = _draw_signature_strokes(pdf, signature_strokes, box_top)
        except Exception:
            drew_vector = False
    if not drew_vector and signature_png:
        # 레거시 서명 이미지 (in-memory) — strokes 없는 구 폼만.
        try:
            pdf.image(
                BytesIO(signature_png),
                x=_SIG_BOX_X + 4, y=box_top + 2,
                w=_SIG_BOX_W - 10, h=_SIG_BOX_H - 6,
                keep_aspect_ratio=True,
            )
        except Exception:
            pass
    # 테두리는 서명 위에 그린다 (선폭/색 복원 후).
    pdf.set_draw_color(180, 180, 180)
    pdf.rect(_SIG_BOX_X, box_top, _SIG_BOX_W, _SIG_BOX_H)
    pdf.set_y(box_top + _SIG_BOX_H + 2)
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
