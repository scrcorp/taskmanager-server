"""스케줄 일일 보고서 PDF — 이메일 본문에서 덜어낸 상세를 담는다 (WeasyPrint).

왜 PDF 인가: 본문이 매장 수에 비례해 무한히 자라서 Gmail 이 ~102KB 에서 잘라먹는다.
실측(2026-08-14 prod)으로 180KB 였고, 이슈가 0건이어도 매장 7개면 이미 문턱에 닿는다.
그래서 **요약은 메일 본문, 상세는 첨부 PDF** 로 나눈다.

이메일 HTML 을 그대로 WeasyPrint 에 먹이지 않는다 — 그 마크업은 메일 클라이언트용
(고정폭 table, :hover CSS)이라 `@page` 에서 페이지 분할이 깨진다. 같은 데이터 모델
(stores/cells/diff)을 인쇄용 레이아웃으로 다시 렌더한다.

WeasyPrint 는 native lib(pango/cairo/gdk-pixbuf)이 필요하다 — Dockerfile 에 이미 있고,
없는 호스트를 위해 호출부(`schedule_report_service`)에 폴백이 있다.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

# 딥링크 URL 스킴은 이메일과 **한 곳에서만** 정의한다. 복제하면 콘솔 쿼리 파라미터가
# 바뀔 때 한쪽만 고쳐지고, 그 사실을 아무도 모른 채 PDF 링크만 죽는다.
from app.utils.email_templates import (
    _daily_view_link,
    _issue_link,
    _weekly_view_link,
)

if TYPE_CHECKING:  # 런타임 순환 import 방지 — 타입 힌트 전용
    from app.services.schedule_report_service import Issue, ShiftCell, StoreInfo

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATE_NAME = "schedule_report/daily_report.html"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)

# 이메일 본문과 라벨이 갈리면 같은 리포트가 두 개의 이름을 갖게 된다.
# 카테고리 추가 시 여기와 email_templates 를 함께 고칠 것.
CATEGORY_TITLES = {
    "shift_understaffed": "Understaffed shifts",
    "sv_gap": "Supervisor coverage gaps",
    "over_6h": "Over 6h (scheduled)",
    "no_break_8h": "No break with 8h+ (scheduled)",
    "att_over_6h": "Over 6h (actual)",
    "att_no_break_8h": "No break with 8h+ (actual)",
}

_CATEGORY_ORDER = list(CATEGORY_TITLES)


def _fmt_day(d: date) -> str:
    return d.strftime("%b %-d (%a)")


def _week_anchor(d: date) -> date:
    """그 날짜가 속한 주의 일요일. 주는 항상 Sun→Sat (프로젝트 규약)."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _store_blocks(
    stores: list["StoreInfo"],
    cells: list["ShiftCell"],
    diff: "ReportDiff",  # noqa: F821
    target_dates: list[date],
    admin_base_url: str,
) -> tuple[list[dict], list[dict]]:
    """매장 하나 = 블록 하나. 그 매장의 매트릭스와 그 매장의 이슈를 붙여서 낸다.

    카테고리별로 모으면 "SV 공백 34건" 같은 총량은 보이지만, 매장 담당자가 자기
    매장만 보려면 여섯 개 섹션을 훑어야 한다. 조치는 매장 단위로 일어나므로
    묶음도 매장 단위여야 한다.

    Returns:
        (store_blocks, orphan_issues) — orphan 은 store_id 가 없는 이슈.
        매장에 못 붙는다고 버리면 조용히 사라지므로 따로 담아 뒤에 붙인다.
    """
    by_store_cells: dict[str, list["ShiftCell"]] = {}
    for c in cells:
        by_store_cells.setdefault(c.store_id, []).append(c)

    # resolved 는 담지 않는다 — 해소된 항목이 목록에 있으면 "아직 남은 문제" 로 오독된다.
    marked = [("NEW", i) for i in diff.new] + [("", i) for i in diff.ongoing]

    def _strip_store(label: str, store_name: str | None) -> str:
        """매장 블록 안에서는 매장명이 군더더기다 — 줄마다 반복되면 정작 이름과 시각이 안 보인다."""
        if not store_name:
            return label
        out = label.replace(f" ({store_name})", "").replace(f"{store_name} ", "", 1)
        return out.strip() or label

    def _date_label(iso: str) -> str:
        try:
            return date.fromisoformat(iso).strftime("%b %-d")
        except Exception:
            return iso

    by_store_issues: dict[str, list] = {}
    orphans: list = []
    for marker, issue in marked:
        (by_store_issues.setdefault(issue.store_id, []) if issue.store_id else orphans).append(
            (marker, issue)
        )

    def _grouped(entries: list, store_name: str | None) -> list[dict]:
        """카테고리별로 묶고, 그 안에서 날짜순. 날짜는 별도 칸으로 세운다.

        예전엔 날짜순으로만 정렬해서 종류가 흩어졌고, 날짜가 라벨 안에 숨어 있어
        같은 사람의 다른 날짜 항목이 중복처럼 보였다.
        """
        buckets: dict[str, list[dict]] = {}
        for marker, issue in entries:
            buckets.setdefault(issue.category, []).append({
                "marker": marker,
                "date": _date_label(issue.target_date),
                "text": _strip_store(issue.label, store_name),
                "href": _issue_link(issue, admin_base_url),
                "_sort": (issue.target_date, issue.label),
            })
        ordered = [c for c in _CATEGORY_ORDER if c in buckets]
        ordered += [c for c in buckets if c not in CATEGORY_TITLES]
        out = []
        for cat in ordered:
            rows = sorted(buckets[cat], key=lambda r: r["_sort"])
            out.append({
                "title": CATEGORY_TITLES.get(cat, cat),
                "count": len(rows),
                "entries": rows,
            })
        return out

    anchor = _week_anchor(target_dates[0]) if target_dates else None

    blocks: list[dict] = []
    for store in stores:
        shifts: dict[str, dict] = {}
        for c in by_store_cells.get(store.id, []):
            shifts.setdefault(
                c.shift_id,
                {"name": c.shift_name, "sort_order": c.shift_sort_order, "by_date": {}},
            )["by_date"][c.target_date] = c

        rows = []
        for shift in sorted(shifts.values(), key=lambda s: (s["sort_order"], s["name"])):
            row_cells = []
            for d in target_dates:
                c = shift["by_date"].get(d)
                if c is None:
                    row_cells.append({"text": "–", "flag": False, "href": None})
                    continue
                # SV 0 명은 인원이 있어도 표시한다 — 커버리지 경고의 근거다.
                flag = c.staff_count == 0 or c.sv_count == 0
                row_cells.append({
                    "text": f"{c.staff_count} ({c.sv_count} SV)",
                    "flag": flag,
                    # 문제가 있는 칸만 링크로 만든다. 전부 링크면 어디를 눌러야 할지 모른다.
                    "href": _daily_view_link(store.id, d.isoformat(), admin_base_url) if flag else None,
                })
            rows.append({"shift_name": shift["name"], "cells": row_cells})

        store_entries = by_store_issues.get(store.id, [])
        blocks.append({
            "store_name": store.name,
            "store_href": _weekly_view_link(store.id, anchor, admin_base_url) if anchor else None,
            "rows": rows,
            "issue_groups": _grouped(store_entries, store.name),
            "issue_count": len(store_entries),
        })

    return blocks, _grouped(orphans, None)


def build_schedule_daily_report_pdf(
    *,
    org_name: str,
    sent_date: date,
    target_dates: list[date],
    yesterday: date | None = None,
    diff: "ReportDiff",  # noqa: F821
    stores: list["StoreInfo"],
    cells: list["ShiftCell"],
    admin_base_url: str,
) -> tuple[str, bytes]:
    """(filename, pdf_bytes) 반환.

    시그니처는 `build_schedule_daily_report_email` 과 동일하게 유지한다 —
    호출부가 kwargs 하나를 만들어 양쪽에 넘길 수 있어야 두 산출물의 숫자가 갈라지지 않는다.
    """
    store_blocks, orphan_issues = _store_blocks(
        stores, cells, diff, target_dates, admin_base_url
    )

    context = {
        "org_name": org_name,
        "sent_date": sent_date,
        "sent_date_label": _fmt_day(sent_date),
        "period_label": f"{target_dates[0].isoformat()} ~ {target_dates[-1].isoformat()}",
        "date_headers": [_fmt_day(d) for d in target_dates],
        "yesterday_label": _fmt_day(yesterday) if yesterday else None,
        "store_blocks": store_blocks,
        "orphan_issues": orphan_issues,
        "counts": {
            "new": len(diff.new),
            "ongoing": len(diff.ongoing),
            "resolved": len(diff.resolved),
        },
        "admin_base_url": (admin_base_url or "").rstrip("/"),
    }

    html = _env.get_template(_TEMPLATE_NAME).render(**context)
    pdf_bytes = HTML(string=html).write_pdf()

    safe_org = "".join(ch for ch in org_name if ch.isalnum()) or "Org"
    filename = f"ScheduleReport_{safe_org}_{sent_date:%Y%m%d}.pdf"
    return filename, pdf_bytes
