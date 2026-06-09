"""EMPLOYEE WARNING NOTICE FORM — PDF generator (fpdf2).

현행 종이 양식(temp/warning_sample.pdf)을 좌표로 재현한다. 양식은 각 칸이 테두리
박스(셀)로 둘러싸인 표 형태이므로, 섹션마다 rect 로 박스를 그린다(콘텐츠를 먼저
렌더한 뒤 측정한 높이로 테두리를 그려 가변 텍스트도 박스 안에 들어가게 한다).

우리가 입력받는 정보만 채우고, 받지 않는 칸(Deadline / Follow-up / 서명)은 양식
그대로 빈 줄로 둔다. First/Second/Other 는 그 직원의 경고 순번으로 자동 체크.
"""

from app.utils.pdf import create_pdf

# 종이 양식 Section 1 의 12개 사유 — code → label (양식 문구 그대로).
CATEGORY_LABELS: dict[str, str] = {
    "tardiness": "Tardiness",
    "damaged_equipment": "Damaged equipment",
    "refusal_overtime": "Refusal to work overtime",
    "absenteeism": "Absenteeism",
    "policy_violation": "Policy violation",
    "insubordination": "Insubordination",
    "rudeness": "Rudeness",
    "fighting": "Fighting",
    "language": "Language",
    "failure_procedure": "Failure to follow procedure",
    "failure_performance": "Failure to meet performance standards",
    "other": "Other",
}

# 양식의 3-컬럼 배치 (좌→우, 위→아래).
_COL1 = ["tardiness", "damaged_equipment", "refusal_overtime", "absenteeism", "policy_violation"]
_COL2 = ["insubordination", "rudeness", "fighting", "language"]
_COL3 = ["failure_procedure", "failure_performance", "other"]

LEFT = 12.0
RIGHT = 198.0
WIDTH = RIGHT - LEFT
PAD = 2.4


def _checkbox(pdf, font: str, x: float, y: float, label: str, checked: bool, *, size: float = 3.7) -> None:
    """체크박스 한 칸 — 사각형 + (체크 시) ✓ + 라벨."""
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.25)
    pdf.rect(x, y, size, size)
    if checked:
        # ✓ — 짧은 하향 스트로크 + 긴 상향 스트로크 (X 아님)
        pdf.set_line_width(0.6)
        pdf.line(x + size * 0.16, y + size * 0.52, x + size * 0.42, y + size * 0.80)
        pdf.line(x + size * 0.42, y + size * 0.80, x + size * 0.88, y + size * 0.16)
        pdf.set_line_width(0.25)
    pdf.set_font(font, "B" if checked else "", 8.9)
    pdf.set_xy(x + size + 1.6, y - 1.1)
    pdf.cell(0, size + 1.6, label)


def _box(pdf, top: float, height: float) -> None:
    """LEFT..RIGHT 너비의 테두리 박스."""
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.3)
    pdf.rect(LEFT, top, WIDTH, height)


def build_warning_notice_pdf(
    *,
    company_name: str,
    ref_no: str,
    employee_name: str,
    manager_name: str,
    warning_date: str,
    ordinal: int,
    categories: list[str],
    details: str,
    corrective_action: str,
) -> bytes:
    """EMPLOYEE WARNING NOTICE FORM PDF bytes 반환 (테두리 박스 표 형식).

    원본 종이 양식과 동일한 US Letter 규격(215.9 x 279.4mm)에 그린다.
    """
    pdf, font = create_pdf("Letter")
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()
    pdf.set_text_color(0, 0, 0)

    y = 12.0

    # ── Header row: 제목 | 회사명 (세로 분리선) ───────────────
    h = 13.0
    _box(pdf, y, h)
    div = LEFT + 128
    pdf.set_draw_color(0, 0, 0)
    pdf.line(div, y, div, y + h)
    pdf.set_font(font, "B", 14)
    pdf.set_xy(LEFT + PAD, y + 3.2)
    pdf.cell(div - LEFT - 2 * PAD, 6, "EMPLOYEE WARNING NOTICE FORM")
    pdf.set_font(font, "B", 9.5)
    pdf.set_xy(div + PAD, y + 4.2)
    pdf.multi_cell(RIGHT - div - 2 * PAD, 4, (company_name or "").upper())
    y += h

    # ── Employee Name | Date (한줄 칸 — 넉넉히) ───────────────
    h = 11.0
    _box(pdf, y, h)
    dcol = LEFT + 128
    pdf.line(dcol, y, dcol, y + h)
    pdf.set_font(font, "", 9.5)
    pdf.set_xy(LEFT + PAD, y + 3.6)
    pdf.cell(30, 4.5, "Employee Name:")
    pdf.set_font(font, "B", 10.5)
    pdf.cell(0, 4.5, employee_name)
    pdf.set_font(font, "", 9.5)
    pdf.set_xy(dcol + PAD, y + 3.6)
    pdf.cell(13, 4.5, "Date:")
    pdf.set_font(font, "B", 10.5)
    pdf.cell(0, 4.5, warning_date)
    y += h

    # ── Manager Name ─────────────────────────────────────────
    h = 11.0
    _box(pdf, y, h)
    pdf.set_font(font, "", 9.5)
    pdf.set_xy(LEFT + PAD, y + 3.6)
    pdf.cell(30, 4.5, "Manager Name:")
    pdf.set_font(font, "B", 10.5)
    pdf.cell(0, 4.5, manager_name)
    y += h

    # ── Warning type checkboxes ──────────────────────────────
    h = 11.0
    _box(pdf, y, h)
    cy = y + 3.7
    _checkbox(pdf, font, LEFT + PAD, cy, "First Warning", ordinal == 1)
    _checkbox(pdf, font, LEFT + 58, cy, "Second Warning", ordinal == 2)
    _checkbox(pdf, font, LEFT + 120, cy, "Other", ordinal >= 3 or ordinal <= 0)
    y += h

    # ── Previous discipline meeting ──────────────────────────
    h = 10.0
    _box(pdf, y, h)
    _checkbox(pdf, font, LEFT + PAD, y + 3.3, "Previous discipline meeting was held on:", False)
    pdf.set_draw_color(120, 120, 120)
    pdf.line(LEFT + 90, y + 6.8, RIGHT - PAD, y + 6.8)
    y += h

    # ── Section 1 — reasons (가변 박스: 렌더 후 측정) ─────────
    box_top = y
    # 섹션 제목 음영 밴드(굵게·크게 구분).
    s1_band = 7.8
    pdf.set_fill_color(223, 227, 234)  # #DFE3EA
    pdf.rect(LEFT, box_top, WIDTH, s1_band, "F")
    pdf.set_font(font, "B", 10.2)
    pdf.set_xy(LEFT + PAD, box_top + 2.0)
    pdf.cell(
        WIDTH - 2 * PAD, 4.5,
        "1. Your behavior/actions have been found unsatisfactory for the following reasons:",
    )
    pdf.set_draw_color(150, 150, 150)
    pdf.line(LEFT, box_top + s1_band, RIGHT, box_top + s1_band)

    cb_top = box_top + s1_band + 2.8
    # col3(긴 라벨)에 더 넓은 폭 배분.
    col_x = [LEFT + PAD, LEFT + 60, LEFT + 116]
    for ci, col in enumerate([_COL1, _COL2, _COL3]):
        ccy = cb_top
        for code in col:
            _checkbox(pdf, font, col_x[ci], ccy, CATEGORY_LABELS.get(code, code), code in set(categories))
            ccy += 5.9
    after_cb = cb_top + 5 * 5.9

    # "Details" 라벨 — 가는 음영 줄로 구분(체크박스와 구분).
    label_y = after_cb + 0.8
    pdf.set_fill_color(238, 240, 244)  # #EEF0F4
    pdf.rect(LEFT, label_y, WIDTH, 6.0, "F")
    pdf.set_font(font, "B", 9.3)
    pdf.set_xy(LEFT + PAD, label_y + 1.5)
    pdf.cell(0, 4, "Details of unsatisfactory behavior/actions:")
    details_top = label_y + 6.0 + 1.2

    # 작성칸(Details/Corrective)은 적당히 cap(너무 크지 않게). Letter 규격으로 채움.
    bottom_top = 250.0
    s2_band = 11.5  # Section 2 머리말 음영 밴드 높이(2줄)
    avail = max(36.0, bottom_top - details_top - 2 * PAD - s2_band)
    details_min = min(26.0, avail * 0.45)
    corrective_min = min(32.0, avail * 0.55)

    pdf.set_xy(LEFT + PAD, details_top)
    pdf.set_font(font, "", 10)
    pdf.multi_cell(WIDTH - 2 * PAD, 5.0, details if details else " ")
    box_bottom = max(pdf.get_y(), details_top + details_min) + PAD
    _box(pdf, box_top, box_bottom - box_top)
    y = box_bottom

    # ── Section 2 — corrective action (가변 박스) ────────────
    box_top = y
    # 섹션 제목 음영 밴드(굵게·크게 구분).
    pdf.set_fill_color(223, 227, 234)  # #DFE3EA
    pdf.rect(LEFT, box_top, WIDTH, s2_band, "F")
    pdf.set_font(font, "B", 9.7)
    pdf.set_xy(LEFT + PAD, box_top + 1.8)
    pdf.multi_cell(
        WIDTH - 2 * PAD, 4.3,
        "2. The following immediate and sustained corrective action must be taken by the "
        "employee. Failure to do so will result in further disciplinary action up to and "
        "including termination.",
    )
    pdf.set_draw_color(150, 150, 150)
    pdf.line(LEFT, box_top + s2_band, RIGHT, box_top + s2_band)
    ca_top = box_top + s2_band + 1.5
    pdf.set_xy(LEFT + PAD, ca_top)
    pdf.set_font(font, "", 10)
    pdf.multi_cell(WIDTH - 2 * PAD, 5.0, corrective_action if corrective_action else " ")
    box_bottom = max(pdf.get_y(), ca_top + corrective_min) + PAD
    _box(pdf, box_top, box_bottom - box_top)
    y = box_bottom

    # ── 3. Deadline (빈칸 — 넉넉히) ──────────────────────────
    h = 12.0
    _box(pdf, y, h)
    pdf.set_font(font, "B", 9.5)
    pdf.set_xy(LEFT + PAD, y + 4.2)
    pdf.cell(26, 4.5, "3. Deadline:")
    pdf.set_draw_color(120, 120, 120)
    pdf.line(LEFT + 28, y + 8.0, RIGHT - PAD, y + 8.0)
    y += h

    # ── 4. Follow-up meeting (빈칸 — 넉넉히) ─────────────────
    h = 12.0
    _box(pdf, y, h)
    pdf.set_xy(LEFT + PAD, y + 4.2)
    pdf.cell(62, 4.5, "4. Follow-up meeting will be held on:")
    pdf.set_draw_color(120, 120, 120)
    pdf.line(LEFT + 64, y + 8.0, RIGHT - PAD, y + 8.0)
    y += h

    # ── Employee Signature | Date + Note (두 서명칸 동일 크기) ─
    sig_h = 18.0
    sdiv = RIGHT - 50
    box_top = y
    _box(pdf, box_top, sig_h)
    pdf.set_font(font, "", 9.5)
    pdf.set_xy(LEFT + PAD, box_top + 4.2)
    pdf.cell(35, 4.5, "Employee Signature:")
    pdf.set_draw_color(120, 120, 120)
    pdf.line(LEFT + 38, box_top + 7.8, sdiv - 4, box_top + 7.8)
    pdf.set_xy(sdiv, box_top + 4.2)
    pdf.cell(13, 4.5, "Date:")
    pdf.line(sdiv + 13, box_top + 7.8, RIGHT - PAD, box_top + 7.8)
    pdf.set_xy(LEFT + PAD, box_top + 11.5)
    pdf.set_font(font, "", 7.3)
    pdf.set_text_color(90, 90, 90)
    pdf.multi_cell(
        WIDTH - 2 * PAD, 3.5,
        "Note: Your signature on this form means that we have discussed the situation. "
        "It doesn't necessarily mean you agree that the infraction occurred.",
    )
    pdf.set_text_color(0, 0, 0)
    y += sig_h

    # ── Manager Signature | Date (동일 크기) ─────────────────
    box_top = y
    _box(pdf, box_top, sig_h)
    pdf.set_font(font, "", 9.5)
    pdf.set_xy(LEFT + PAD, box_top + 4.2)
    pdf.cell(36, 4.5, "Manager's Signature:")
    pdf.set_draw_color(120, 120, 120)
    pdf.line(LEFT + 39, box_top + 7.8, sdiv - 4, box_top + 7.8)
    pdf.set_xy(sdiv, box_top + 4.2)
    pdf.cell(13, 4.5, "Date:")
    pdf.line(sdiv + 13, box_top + 7.8, RIGHT - PAD, box_top + 7.8)
    y += sig_h

    # ── cc (+ Ref 우측) ──────────────────────────────────────
    h = 9.0
    _box(pdf, y, h)
    pdf.set_font(font, "", 9)
    pdf.set_xy(LEFT + PAD, y + 2.8)
    pdf.cell(0, 4.5, "cc:   Employee    /    Manager    /    Human Resources    /    Personnel File")
    pdf.set_text_color(150, 150, 150)
    pdf.set_font(font, "", 7.5)
    pdf.set_xy(RIGHT - 46, y + 2.8)
    pdf.cell(46 - PAD, 4.5, f"Ref {ref_no}", align="R")
    pdf.set_text_color(0, 0, 0)
    y += h

    # ── Footer note (박스 밖, 작게) ──────────────────────────
    pdf.set_xy(LEFT, y + 3.0)
    pdf.set_font(font, "", 7.2)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(
        WIDTH, 3.4,
        "Note: This document is for informational purposes only and may not be appropriate for "
        "your situation. Please consult an attorney for all legal matters.",
        align="C",
    )
    pdf.set_text_color(0, 0, 0)

    out = pdf.output(dest="S")
    return bytes(out)
