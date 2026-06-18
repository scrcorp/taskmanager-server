"""경고(Warning) 문서 PDF — 콘솔 폼(WarningFormDoc)을 그대로 서버 렌더(WeasyPrint).

화면 폼은 CSS Grid라 인쇄에서 페이지 분할이 안 된다. 그래서 **같은 폼을** 페이지
분할되는 구조(테두리 table + block)로 서버에서 재현해 PDF로 만든다. 디자인은 폼과
동일하게 맞추고, Subject(제목)는 화면 전용이라 PDF 에선 숨긴다.

WeasyPrint 는 native lib(pango/cairo/gdk-pixbuf) 필요 — requirements.txt 주석 참조.
입력은 build_warning_response() dict + (사유 체크리스트용) org 카테고리 옵션 목록.
"""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from weasyprint import HTML

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATE_NAME = "warnings/warning_document.html"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)

_ORDINAL_SUFFIX = {1: "st", 2: "nd", 3: "rd"}
_VB_W = 1000  # SignatureView 와 동일한 viewBox 폭
_PX_PER_MM = 96 / 25.4  # WeasyPrint 내부 단위(CSS px) → mm
_FILL_SAFETY_MM = 5.0  # 측정 후 바닥에 안 닿게 남길 여백
_FILL_BACKOFF_ITERS = 3  # 측정 오차로 넘쳤을 때 보수적 back off 횟수


def _fmt_date(d: date | None) -> str:
    return d.strftime("%b %-d, %Y") if isinstance(d, date) else ""


def _fmt_time(t) -> str:
    """'HH:MM' 문자열 또는 datetime.time → 'h:MM AM/PM' (없음/파싱실패 → '').
    DB(TIME 컬럼)는 datetime.time 으로 주므로 둘 다 처리한다."""
    if not t:
        return ""
    if isinstance(t, time):
        return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"
    try:
        hh, mm = (int(x) for x in str(t).split(":")[:2])
        return f"{hh % 12 or 12}:{mm:02d} {'AM' if hh < 12 else 'PM'}"
    except (ValueError, TypeError):
        return str(t)


def _fmt_dt(dt) -> str:
    """datetime|date → 'Jun 17, 2026' (서명일)."""
    if isinstance(dt, datetime):
        return _fmt_date(dt.date())
    return _fmt_date(dt) if isinstance(dt, date) else ""


def _coord(p, i: int) -> float:
    """point p 의 i번째 좌표를 float 로 안전 추출. JS(SignatureView)의 `?? 0` 처럼
    관대하게 — 점이 [x,y] 가 아니거나 좌표가 None/비숫자여도 0 으로(크래시 금지)."""
    try:
        v = p[i]
    except (TypeError, IndexError, KeyError):
        return 0.0
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _stroke_to_path(stroke, w: float, h: float) -> str:
    if not isinstance(stroke, list) or not stroke:
        return ""
    if len(stroke) == 1:
        x, y = _coord(stroke[0], 0) * w, _coord(stroke[0], 1) * h
        return f"M {x:.2f} {y:.2f} L {x + 0.5:.2f} {y:.2f}"
    return " ".join(
        f"{'M' if i == 0 else 'L'} {_coord(p, 0) * w:.2f} {_coord(p, 1) * h:.2f}"
        for i, p in enumerate(stroke)
    )


def _signature_svg(payload, *, color: str = "#1A1C22", stroke_width: float = 2.6) -> str:
    """정규화 벡터 서명({strokes, aspect}) → inline SVG. SignatureView 포팅.
    어떤 stroke 데이터(결손/형식오류)에도 절대 예외를 던지지 않는다(빈 문자열 폴백)."""
    try:
        if not isinstance(payload, dict):
            return ""
        strokes = payload.get("strokes")
        if not isinstance(strokes, list) or not strokes:
            return ""
        try:
            aspect = float(payload.get("aspect"))
        except (TypeError, ValueError):
            aspect = 2.6
        if aspect <= 0:
            aspect = 2.6
        vb_h = _VB_W / aspect

        xs: list[float] = []
        ys: list[float] = []
        for s in strokes:
            if not isinstance(s, list):
                continue
            for p in s:
                xs.append(_coord(p, 0))
                ys.append(_coord(p, 1))
        if not xs:
            return ""
        dx = (0.5 - (min(xs) + max(xs)) / 2) * _VB_W
        dy = (0.5 - (min(ys) + max(ys)) / 2) * vb_h

        paths = "".join(
            f'<path d="{_stroke_to_path(s, _VB_W, vb_h)}" />'
            for s in strokes
            if isinstance(s, list) and s
        )
        if not paths:
            return ""
    except Exception:
        return ""
    return (
        f'<svg viewBox="0 0 {_VB_W} {vb_h:.2f}" preserveAspectRatio="xMidYMid meet" '
        f'style="height:42px;width:auto;max-width:100%">'
        f'<g transform="translate({dx:.2f} {dy:.2f})" fill="none" stroke="{color}" '
        f'stroke-width="{stroke_width}" stroke-linecap="round" stroke-linejoin="round">'
        f"{paths}</g></svg>"
    )


def _sig_view(sig: dict | None, fallback_name: str | None) -> dict | None:
    """digital 서명 dict → 템플릿용 {svg(Markup), name, date}. 미서명이면 None."""
    if not sig:
        return None
    svg = _signature_svg(sig.get("signature_strokes"))
    if not svg:
        return None
    return {
        "svg": Markup(svg),
        "name": sig.get("signer_name") or fallback_name or "—",
        "date": _fmt_dt(sig.get("signed_at")) or "Date",
    }


class WarningPdfService:
    """경고 응답 dict(+카테고리 옵션) → 폼 재현 PDF 바이트."""

    def render_document(self, data: dict, categories: list[dict] | None = None):
        """warning dict → WeasyPrint Document. short 경고는 한 페이지를 꽉 채우되
        **절대 다음 페이지로 넘기지 않게** 본문(1·2)의 빈 높이를 measure-and-fit 으로
        맞춘다. categories=org 카테고리 옵션(사유 체크리스트)."""
        return self._fit(self._build_context(data, categories))

    def render_pdf(self, data: dict, categories: list[dict] | None = None) -> bytes:
        return self.render_document(data, categories).write_pdf()

    def _render(self, ctx: dict, fill_mm: float = 0.0):
        """ctx + 채우기 높이(mm) → Document. fill 은 본문 1(60%)·2(40%)에 분배."""
        c = {**ctx, "grow1": round(fill_mm * 0.6, 1), "grow2": round(fill_mm * 0.4, 1)}
        html = _env.get_template(_TEMPLATE_NAME).render(**c)
        return HTML(string=html).render()

    def _avail_mm(self, doc) -> float:
        """마지막 페이지에서 본문 아래로 남은 빈 공간(mm). 1페이지 문서용.
        margin box(페이지번호 등) 제외하고 in-flow 콘텐츠 바닥만 잰다. 실패 시 0."""
        try:
            pb = doc.pages[-1]._page_box
            flow = [c for c in pb.children if type(c).__name__ != "MarginBox"]
            if not flow:
                return 0.0
            top = min(c.position_y for c in flow)
            bottom = max(c.position_y + c.margin_height() for c in flow)
            return max(0.0, ((top + pb.height) - bottom) / _PX_PER_MM)
        except Exception:
            return 0.0

    def _fit(self, ctx: dict):
        """natural(fill=0)이 1페이지면 남은 공간을 측정해 본문(1·2) 빈칸을 그만큼만
        채운다(2회 렌더). 측정 오차로 넘치면 보수적으로 back off. 이미 여러 페이지면
        채우지 않고 자연 분할 — 거의 빈 페이지를 강제로 만들지 않는다."""
        base = self._render(ctx, 0.0)
        if len(base.pages) > 1:
            return base
        avail = self._avail_mm(base)
        if avail < 8:
            return base
        fill = avail - _FILL_SAFETY_MM
        doc = self._render(ctx, fill)
        if len(doc.pages) == 1:
            return doc
        # 측정 오차로 넘쳤다 → 이분 back off (절대 2페이지로 안 남기게)
        lo, hi, best = 0.0, fill, base
        for _ in range(_FILL_BACKOFF_ITERS):
            mid = (lo + hi) / 2
            d = self._render(ctx, mid)
            if len(d.pages) == 1:
                best, lo = d, mid
            else:
                hi = mid
        return best

    def _build_context(self, data: dict, categories: list[dict] | None) -> dict:
        chosen = set(data.get("categories") or [])
        labels: dict = data.get("category_labels") or {}

        # 사유 체크리스트 — org 전체 옵션(체크 표시) + 선택됐지만 사라진 legacy(취소선).
        if categories is None:
            reasons = [{"label": labels.get(c, c), "on": True, "removed": False} for c in chosen]
        else:
            active = {o["code"] for o in categories}
            reasons = [
                {"label": o["label"], "on": o["code"] in chosen, "removed": False}
                for o in categories
            ]
            reasons += [
                {"label": labels.get(c, c), "on": True, "removed": True}
                for c in chosen
                if c not in active
            ]

        signatures = data.get("signatures") or {}
        is_wet = data.get("signature_method") == "wet"

        follow_up = " · ".join(
            p for p in (_fmt_date(data.get("follow_up_date")), _fmt_time(data.get("follow_up_time"))) if p
        )

        return {
            "ref_no": data.get("ref_no"),
            "store_name": data.get("store_name"),
            "warning_date": _fmt_date(data.get("warning_date")),
            "employee_no": data.get("employee_no"),
            "employee_name": data.get("subject_name"),
            "manager_name": data.get("issued_by_name"),
            "ordinal": data.get("ordinal"),
            "reasons": reasons,
            "details": data.get("details"),
            "corrective_action": data.get("corrective_action"),
            "deadline": _fmt_date(data.get("deadline")),
            "follow_up": follow_up,
            "is_wet": is_wet,
            "wet_signed": bool(data.get("signed_pdf_present")),
            "emp_sig": None if is_wet else _sig_view(signatures.get("employee"), data.get("subject_name")),
            "mgr_sig": None if is_wet else _sig_view(signatures.get("manager"), data.get("issued_by_name")),
        }


warning_pdf_service: WarningPdfService = WarningPdfService()
