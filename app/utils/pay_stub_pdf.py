"""Pay stub (itemized wage statement) PDF — Payroll v1 Phase 4 (E4).

CA Labor Code 226(a) 항목을 동결된 payroll entry 의 breakdown(calc_version=1)
으로 렌더한다. fpdf2 사용 (WeasyPrint 네이티브 라이브러리 없는 환경 대응 —
form_4070_pdf 와 같은 패턴).

226(a) 매핑 (v1 스코프):
    (1) gross wages          → entry.gross_pay (reg/ot/dt + penalty + card tips 합)
    (2) total hours worked   → reg+ot+dt minutes 합 (시간 표시)
    (3) piece-rate           → 해당 없음 (시급제)
    (4) deductions           → v1 미계산 — 고정 문구 "handled by payroll provider"
    (5) net wages            → v1 미계산 — provider 산출 안내 문구로 대체 (표기 생략)
    (6) pay period dates     → period start/end
    (7) employee name + ID   → member_name + EMPID/CREWID 스냅샷 (SSN 미보관 — 미표기)
    (8) employer legal name/address
        → GAP: Organization 에 법인명(legal name)/주소 필드가 없다.
          org.name(상호) + store.address(매장 주소)로 대체 표기.
          별도 employer legal entity 필드 추가는 후속 결정 사항.
    (9) hourly rates + hours at each rate
        → breakdown.segments (rate 별 reg/ot/dt 분 + 귀속 금액)

추가 표기 (C5): meal/rest penalty 라인은 payroll_events.reason 스냅샷(사유)을
그대로 노출한다. sick leave 잔액은 별도 문서 안내 문구 (스펙 §9 placeholder).

Daily detail (일자별 상세): breakdown.days 를 날짜별 표로 낸다 — 시간/적용
rate + 그날 금액(days[].total_amount). 금액 필드가 생기기 전 동결된 entry 는
그 열이 "—" 로 비고, 재계산하지 않는다. 일별 금액은 하루 단위 반올림된
정보값이라 rate 표/합계가 여전히 authoritative (표 아래 주석으로 명시).

UI 텍스트 규칙: 영어 전용.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from app.utils.pdf import create_pdf

if TYPE_CHECKING:  # 순환/무거운 import 회피 — 런타임엔 duck-typed ORM 객체
    from datetime import date

    from app.models.organization import Organization, Store
    from app.models.payroll import PayPeriod, PayrollEntry

# penalty kind → 사람이 읽는 라벨 (payroll_events.kind)
_PENALTY_LABELS = {
    "meal_penalty": "Meal period premium",
    "rest_penalty": "Rest break premium",
}

_ACCENT = (37, 99, 235)  # #2563EB — daily report PDF 와 동일 계열
_MUTED = (100, 116, 139)
_INK = (30, 41, 59)

# 값 없음 표기 — 옛 동결본의 일별 금액/미상 rate (빈칸은 조용한 누락으로 읽힌다)
_DASH = "—"

# Daily detail 표 열 폭 (mm) — date, reg, ot, dt, rate, amount(나머지)
_DAY_COL_W = (30, 26, 26, 26, 30, 0)


def _hours(minutes: int) -> str:
    """분 → 시간 표기 (소수 2자리). 226(a)(2)/(9) hours 표기용."""
    return f"{minutes / 60:.2f}"


def _money(value: Decimal | int | float | str) -> str:
    return f"$ {Decimal(str(value)):,.2f}"


def day_label(work_date: date) -> str:
    """일자별 표의 날짜 셀 — "Jul 20 (Mon)" (콘솔 Day detail 과 같은 표기).

    요일이 붙어야 주 단위 규칙(주 40h·7일 연속)이 왜 그 날 걸렸는지 읽힌다.
    """
    return f"{work_date.strftime('%b')} {work_date.day} ({work_date.strftime('%a')})"


def _section(pdf, font: str, title: str) -> None:
    pdf.ln(2)
    pdf.set_font(font, "B", 11)
    pdf.set_text_color(*_ACCENT)
    pdf.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_INK)


def _kv(pdf, font: str, label: str, value: str) -> None:
    pdf.set_font(font, "B", 10)
    pdf.set_text_color(*_MUTED)
    pdf.cell(48, 6, label, new_x="RIGHT")
    pdf.set_font(font, "", 10)
    pdf.set_text_color(*_INK)
    pdf.multi_cell(0, 6, value, new_x="LMARGIN", new_y="NEXT")


DRAFT_STUB_BANNER = "DRAFT — period not confirmed; numbers may change"


def build_pay_stub_pdf(
    entry: "PayrollEntry",
    period: "PayPeriod",
    store: "Store",
    org: "Organization",
    *,
    draft: bool = False,
) -> bytes:
    """동결 entry (또는 draft=True 로 preview 행 유사 객체) → pay stub PDF bytes.

    draft: open 기간 preview 기반 임시 명세서 — 1장 상단 DRAFT 배너 + 출처
        문구가 '확정 전 미리보기'로 바뀐다. 저장하지 않는 즉석 생성 전용.

    Raises:
        BadRequestError: breakdown 이 파싱 불가하거나 calc_version 이 다른 경우
            (parse_frozen_breakdown 의 계약 검증 그대로 — 조용한 오독 방지).
    """
    # 지연 import — utils → services 순환 방지. breakdown 계약 검증은
    # parse_frozen_breakdown 단일 원천 (calc_version 가드 포함).
    from app.services.payroll_calc_service import parse_frozen_breakdown

    breakdown = parse_frozen_breakdown(entry.breakdown)

    pdf, font = create_pdf()
    pdf.add_page()

    # ── 헤더 밴드 ────────────────────────────────────────────
    pdf.set_fill_color(*_ACCENT)
    pdf.rect(0, 0, 210, 22, "F")
    pdf.set_font(font, "B", 16)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(14, 5)
    pdf.cell(0, 6, "Itemized Wage Statement", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font, "", 9)
    pdf.set_x(14)
    pdf.cell(0, 5, "Pay stub — California Labor Code section 226(a)",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_INK)
    pdf.set_y(28)

    if draft:
        # draft export 배너와 같은 문구 톤 — 확정본과 절대 안 헷갈리게
        pdf.set_font(font, "B", 10)
        pdf.set_text_color(180, 95, 6)
        pdf.cell(0, 6, DRAFT_STUB_BANNER, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*_INK)
        pdf.ln(1)

    # ── Employer (226(a)(8)) ─────────────────────────────────
    _section(pdf, font, "Employer")
    _kv(pdf, font, "Legal name", org.name)
    address = (getattr(store, "address", None) or "").strip()
    if address:
        _kv(pdf, font, "Address", f"{store.name} — {address}")
    else:
        # GAP: org/store 주소 미입력 — 빈칸 대신 상태를 명시 (조용한 누락 방지)
        _kv(pdf, font, "Address", f"{store.name} — address not on file")

    # ── Employee (226(a)(7)) — SSN 미보관, EMPID/CREWID 만 ──
    _section(pdf, font, "Employee")
    _kv(pdf, font, "Name", entry.member_name)
    ids = []
    if entry.empid is not None:
        ids.append(f"EMPID {entry.empid}")
    if entry.crewid is not None:
        ids.append(f"CREWID {entry.crewid}")
    _kv(pdf, font, "Employee ID", " / ".join(ids) if ids else "not assigned")

    # ── Pay period (226(a)(6)) ───────────────────────────────
    _section(pdf, font, "Pay period")
    _kv(
        pdf, font, "Dates",
        f"{period.start_date.isoformat()} - {period.end_date.isoformat()}",
    )

    # ── Hourly rates + hours at each rate (226(a)(9)) ────────
    _section(pdf, font, "Hourly rates and hours worked")
    col_w = (30, 28, 28, 28, 0)  # rate, reg, ot, dt, amount
    pdf.set_font(font, "B", 9)
    pdf.set_text_color(*_MUTED)
    pdf.cell(col_w[0], 6, "Rate")
    pdf.cell(col_w[1], 6, "Regular hrs", align="R")
    pdf.cell(col_w[2], 6, "OT hrs (1.5x)", align="R")
    pdf.cell(col_w[3], 6, "DT hrs (2x)", align="R")
    pdf.cell(col_w[4], 6, "Amount", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font, "", 10)
    pdf.set_text_color(*_INK)
    for seg in breakdown.segments:
        rate_label = _money(seg.rate) + "/hr" if seg.rate > 0 else "rate unknown"
        pdf.cell(col_w[0], 6, rate_label)
        pdf.cell(col_w[1], 6, _hours(seg.regular_minutes), align="R")
        pdf.cell(col_w[2], 6, _hours(seg.ot_minutes), align="R")
        pdf.cell(col_w[3], 6, _hours(seg.dt_minutes), align="R")
        pdf.cell(col_w[4], 6, _money(seg.amount), align="R",
                 new_x="LMARGIN", new_y="NEXT")
    if not breakdown.segments:
        pdf.cell(0, 6, "No hours worked in this period",
                 new_x="LMARGIN", new_y="NEXT")

    # OT premium 주석 — 멀티 rate 주는 가중평균 base (계산 규칙 1)
    if any(s.ot_minutes or s.dt_minutes for s in breakdown.segments):
        pdf.set_font(font, "", 8)
        pdf.set_text_color(*_MUTED)
        pdf.multi_cell(
            0, 4,
            "Overtime (1.5x) and double-time (2x) premiums use the weighted-average "
            "regular rate for weeks with multiple rates.",
            new_x="LMARGIN", new_y="NEXT",
        )
        pdf.set_text_color(*_INK)

    # ── Earnings summary ─────────────────────────────────────
    _section(pdf, font, "Earnings")
    _earning_row(pdf, font, f"Regular ({_hours(entry.regular_minutes)} hrs)",
                 entry.regular_pay)
    _earning_row(pdf, font, f"Overtime 1.5x ({_hours(entry.ot_minutes)} hrs)",
                 entry.ot_pay)
    _earning_row(pdf, font, f"Double time 2x ({_hours(entry.dt_minutes)} hrs)",
                 entry.dt_pay)
    _earning_row(pdf, font, "Meal/rest premiums", entry.penalty_pay)
    _earning_row(pdf, font, "Card tips", entry.card_tips)

    # ── Penalty lines with reasons (C5) ──────────────────────
    if breakdown.penalties:
        _section(pdf, font, "Meal and rest period premiums")
        for pen in breakdown.penalties:
            label = _PENALTY_LABELS.get(pen.kind, pen.kind)
            pdf.set_font(font, "B", 9)
            pdf.cell(150, 5, f"{pen.work_date.isoformat()} — {label}")
            pdf.set_font(font, "", 9)
            pdf.cell(0, 5, _money(pen.amount), align="R",
                     new_x="LMARGIN", new_y="NEXT")
            pdf.set_font(font, "", 8)
            pdf.set_text_color(*_MUTED)
            pdf.multi_cell(
                0, 4, f"Reason: {pen.reason}", new_x="LMARGIN", new_y="NEXT"
            )
            pdf.set_text_color(*_INK)

    # ── Totals (226(a)(1)/(2)) ───────────────────────────────
    total_minutes = entry.regular_minutes + entry.ot_minutes + entry.dt_minutes
    _section(pdf, font, "Totals")
    pdf.set_font(font, "B", 11)
    pdf.set_fill_color(239, 246, 255)
    pdf.cell(120, 8, f"Total hours worked: {_hours(total_minutes)}", fill=True)
    pdf.cell(0, 8, f"Gross wages: {_money(entry.gross_pay)}", align="R",
             fill=True, new_x="LMARGIN", new_y="NEXT")

    # ── Deductions / net wages / sick leave (v1 고정 문구) ──
    _section(pdf, font, "Deductions and net wages")
    pdf.set_font(font, "", 10)
    pdf.cell(0, 6, "Deductions: handled by payroll provider",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font, "", 9)
    pdf.set_text_color(*_MUTED)
    pdf.multi_cell(
        0, 5,
        "Net wages are calculated by the payroll provider after tax withholding "
        "and other deductions, and are shown on the provider's statement.",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.multi_cell(
        0, 5,
        "Paid sick leave balance is provided in a separate document.",
        new_x="LMARGIN", new_y="NEXT",
    )

    # ── Footer — 1장 끝. 출처 문구까지 1장에 둬야 요약본만 떼어도 완결된다.
    pdf.ln(4)
    pdf.set_font(font, "", 8)
    pdf.set_text_color(148, 163, 184)
    provenance = (
        "Generated from a LIVE PREVIEW of an unconfirmed pay period — figures "
        "are not final until the period is confirmed"
        if draft
        else "Generated from confirmed payroll records"
    )
    pdf.multi_cell(
        0, 4,
        f"{provenance} (calc v{entry.calc_version}, "
        f"revision {entry.revision}). This statement itemizes wages for the pay "
        "period above and accompanies the payroll provider's paycheck record. "
        "A day-by-day breakdown follows on the next page.",
        new_x="LMARGIN", new_y="NEXT",
    )

    # ── Daily detail — 1장은 요약본으로 완결, 상세는 항상 2장부터 (사용자 결정).
    period_label = f"{period.start_date.isoformat()} - {period.end_date.isoformat()}"
    _day_detail_section(pdf, font, breakdown, entry.member_name, period_label)

    return bytes(pdf.output())


def _earning_row(pdf, font: str, label: str, amount: Decimal) -> None:
    pdf.set_font(font, "", 10)
    pdf.cell(120, 6, label)
    pdf.cell(0, 6, _money(amount), align="R", new_x="LMARGIN", new_y="NEXT")


def worked_times_line(day) -> str:
    """그날 근무/휴게 벽시계 한 줄 — "Worked 08:00-16:30 · Meal … · Rest …".

    기록이 없으면 빈 문자열 (옛 동결본·전기 frozen 소스 일자). 무급 식사는
    구간 그대로, 유급 휴게는 시작 시각만 — 10분짜리 종료 시각은 노이즈다.
    """
    parts: list[str] = []
    worked = [
        f"{s.start}-{s.end}" if s.end else f"{s.start}-"
        for s in getattr(day, "shifts", []) or []
    ]
    if worked:
        parts.append("Worked " + ", ".join(worked))

    meals = [
        f"{b.start}-{b.end}" if b.end else b.start
        for b in getattr(day, "breaks", []) or []
        if b.type == "unpaid_meal"
    ]
    if meals:
        parts.append("Meal " + ", ".join(meals))

    rests = [
        b.start for b in getattr(day, "breaks", []) or [] if b.type != "unpaid_meal"
    ]
    if rests:
        parts.append("Rest " + ", ".join(rests))
    return " · ".join(parts)


def day_premium_total(penalties, work_date) -> Decimal:
    """그날 meal/rest premium 합계 — breakdown.penalties 에서 파생 (스키마 무증설).

    일별 premium 을 breakdown 에 따로 저장하지 않는다: penalties[] 가 이미
    날짜별 사유+금액을 갖고 있어 파생값이고, 동결 계약을 키우면 옛 entry 와
    새 entry 가 서로 다른 진실을 갖게 된다.
    """
    return sum(
        (p.amount for p in penalties or [] if p.work_date == work_date),
        Decimal("0.00"),
    )


def day_amount_parts(day, premium: Decimal) -> list[tuple[str, Decimal]]:
    """그날 금액 내역 — (라벨, 금액) 목록. **0 인 항목은 빼고**, 없으면 빈 목록.

    옛 동결본(금액 필드 없음)은 근무 금액을 알 수 없으므로 빈 목록 —
    premium 만 있는 상태로 반쪽 내역을 보여주지 않는다.
    """
    if day.total_amount is None:
        return []
    candidates = [
        ("Regular", day.regular_amount),
        ("OT", day.ot_amount),
        ("DT", day.dt_amount),
        ("Premium", premium),
    ]
    return [
        (label, amount)
        for label, amount in candidates
        if amount is not None and amount != 0
    ]


def day_amounts_line(day, premium: Decimal) -> str:
    """금액 내역 서브라인 — "Regular $ 104.00 · OT $ 13.00 · Premium $ 36.00"."""
    parts = day_amount_parts(day, premium)
    return " · ".join(f"{label} {_money(amount)}" for label, amount in parts)


def day_total(day, premium: Decimal) -> Optional[Decimal]:
    """그날 실지급 합 = 근무 금액(reg+ot+dt) + premium. 옛 동결본은 None.

    카드팁은 기간 단위 집계라 일별에 넣지 않는다 (Earnings/Totals 에만).
    """
    if day.total_amount is None:
        return None
    return day.total_amount + premium


def context_days_note(context_days) -> str:
    """경계 걸친 주 각주 — 왜 이 기간에 OT 가 걸렸는지 명세서 안에서 읽히게."""
    if not context_days:
        return ""
    dates = sorted(c.work_date for c in context_days)
    hours = sum(c.net_minutes for c in context_days) / 60
    span = (
        f"{day_label(dates[0])}"
        if len(dates) == 1
        else f"{day_label(dates[0])} - {day_label(dates[-1])}"
    )
    return (
        f"Includes {len(dates)} day(s) worked in the prior period ({span}, "
        f"{hours:.2f}h) counted toward the weekly 40h threshold; those days are "
        "paid on the prior period's statement."
    )


def _stub_label(pdf, font: str, label: str) -> None:
    """상세 페이지 상단의 짧은 반복 헤더 — 직원명 + 기간 (낱장 식별용)."""
    pdf.set_font(font, "", 9)
    pdf.set_text_color(*_MUTED)
    pdf.cell(0, 5, label, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_INK)


def _day_table_header(pdf, font: str) -> None:
    """Daily detail 열 제목 — 페이지가 넘어가면 새 장 맨 위에 다시 그린다."""
    pdf.set_font(font, "B", 9)
    pdf.set_text_color(*_MUTED)
    pdf.cell(_DAY_COL_W[0], 6, "Date")
    pdf.cell(_DAY_COL_W[1], 6, "Regular hrs", align="R")
    pdf.cell(_DAY_COL_W[2], 6, "OT hrs", align="R")
    pdf.cell(_DAY_COL_W[3], 6, "DT hrs", align="R")
    pdf.cell(_DAY_COL_W[4], 6, "Rate", align="R")
    pdf.cell(_DAY_COL_W[5], 6, "Day total", align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_INK)


def _day_detail_section(
    pdf, font: str, breakdown, member_name: str, period_label: str
) -> None:
    """일자별 상세 표 — Date | Regular | OT | DT | Rate | Daily amount.

    항상 새 페이지에서 시작한다 — 1장 = 요약본(합계·rate표·법정 문구),
    2장부터 = 상세 데이터 (사용자 결정). 장이 분리되므로 상단에 직원명·기간을
    반복해 어느 명세서의 상세인지 단독으로도 알 수 있게 한다.

    금액 열은 days[].total_amount (그날 reg+ot+dt). 이 필드가 없던 시절 동결된
    entry 는 "—" — 옛 명세서를 재계산하지 않는다.

    그날 penalty 는 해당 일 행 아래에 사유+금액으로 덧붙인다. 전체 목록은 1장
    premium 섹션이 원천이다 (지급 일 행이 없는 penalty 도 거기엔 남는다).

    표가 길면 create_pdf 의 auto page break(margin 20)가 3장 이후로 잇는다.
    """
    stub_label = f"{member_name} - Pay period {period_label}"
    pdf.add_page()
    _stub_label(pdf, font, stub_label)
    _section(pdf, font, "Daily detail")
    _day_table_header(pdf, font)

    if not breakdown.days:
        pdf.set_font(font, "", 10)
        pdf.cell(0, 6, "No daily records in this pay period",
                 new_x="LMARGIN", new_y="NEXT")
        return

    penalties_by_date: dict = {}
    for pen in breakdown.penalties:
        penalties_by_date.setdefault(pen.work_date, []).append(pen)

    legacy_days = False
    grand_total = Decimal("0.00")
    total_reg = total_ot = total_dt = 0
    for day in breakdown.days:
        rate = day.applied_rate
        if pdf.will_page_break(6):
            # 다음 장으로 넘어가는 행 — 어느 명세서인지 + 열 제목을 다시 얹는다
            # (장이 흩어져도 낱장만으로 식별되게)
            pdf.add_page()
            _stub_label(pdf, font, f"{stub_label} (continued)")
            _day_table_header(pdf, font)
        pdf.set_font(font, "", 10)
        pdf.cell(_DAY_COL_W[0], 6, day_label(day.work_date))
        pdf.cell(_DAY_COL_W[1], 6, _hours(day.regular_minutes), align="R")
        pdf.cell(_DAY_COL_W[2], 6, _hours(day.ot_minutes), align="R")
        pdf.cell(_DAY_COL_W[3], 6, _hours(day.dt_minutes), align="R")
        pdf.cell(
            _DAY_COL_W[4], 6,
            f"{_money(rate)}/hr" if rate is not None and rate > 0 else _DASH,
            align="R",
        )
        premium = day_premium_total(breakdown.penalties, day.work_date)
        total = day_total(day, premium)
        if total is None:
            legacy_days = True
        else:
            grand_total += total
            total_reg += day.regular_minutes
            total_ot += day.ot_minutes
            total_dt += day.dt_minutes
        pdf.cell(
            _DAY_COL_W[5], 6,
            _money(total) if total is not None else _DASH,
            align="R", new_x="LMARGIN", new_y="NEXT",
        )

        times = worked_times_line(day)
        if times:
            pdf.set_font(font, "", 8)
            pdf.set_text_color(*_MUTED)
            pdf.set_x(pdf.l_margin + 4)
            pdf.multi_cell(0, 4, times, new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(*_INK)

        # 금액 내역 — Day total 이 무엇으로 이뤄졌는지 (0 인 항목은 생략)
        amounts = day_amounts_line(day, premium)
        if amounts:
            pdf.set_font(font, "", 8)
            pdf.set_text_color(*_MUTED)
            pdf.set_x(pdf.l_margin + 4)
            pdf.multi_cell(0, 4, amounts, new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(*_INK)

        for pen in penalties_by_date.get(day.work_date, []):
            label = _PENALTY_LABELS.get(pen.kind, pen.kind)
            pdf.set_font(font, "", 8)
            pdf.set_text_color(*_MUTED)
            pdf.set_x(pdf.l_margin + 4)
            pdf.multi_cell(
                0, 4,
                f"+ {label} {_money(pen.amount)} — {pen.reason}",
                new_x="LMARGIN", new_y="NEXT",
            )
            pdf.set_text_color(*_INK)

    # ── 합계 행 — 일별 Day total 의 합 (근무 금액 + premium) ────────
    if pdf.will_page_break(8):
        pdf.add_page()
        _stub_label(pdf, font, f"{stub_label} (continued)")
        _day_table_header(pdf, font)
    pdf.set_font(font, "B", 10)
    pdf.set_fill_color(239, 246, 255)
    pdf.cell(_DAY_COL_W[0], 7, "Total", fill=True)
    pdf.cell(_DAY_COL_W[1], 7, _hours(total_reg), align="R", fill=True)
    pdf.cell(_DAY_COL_W[2], 7, _hours(total_ot), align="R", fill=True)
    pdf.cell(_DAY_COL_W[3], 7, _hours(total_dt), align="R", fill=True)
    pdf.cell(_DAY_COL_W[4], 7, _DASH, align="R", fill=True)
    pdf.cell(
        _DAY_COL_W[5], 7,
        _money(grand_total) if not legacy_days or grand_total else _DASH,
        align="R", fill=True, new_x="LMARGIN", new_y="NEXT",
    )

    pdf.set_font(font, "", 8)
    pdf.set_text_color(*_MUTED)
    note = (
        "Day total = regular + overtime + double time + meal/rest premiums for "
        "that day (card tips are period-level and appear in Earnings). Daily "
        "amounts and this total are informational and rounded per day — the rate "
        "table and totals above are the authoritative figures."
    )
    if legacy_days:
        note += (
            " A dash means the pay period was confirmed before daily amounts "
            "were recorded."
        )
    pdf.multi_cell(0, 4, note, new_x="LMARGIN", new_y="NEXT")

    # 경계 걸친 주 각주 — 이 기간 밖 근무가 주 40h 판정에 들어갔다는 사실
    straddle = context_days_note(breakdown.context_days)
    if straddle:
        pdf.ln(1)
        pdf.multi_cell(0, 4, straddle, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_INK)
