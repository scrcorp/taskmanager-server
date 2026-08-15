"""Unit tests — 스케줄 일일 보고서 PDF 렌더러.

WeasyPrint 로 렌더하므로 native lib(pango/cairo)이 필요하다. Docker 이미지에는 있고,
없는 개발 머신에서는 import 단계에서 스킵한다 — 그 경우 서비스 쪽 폴백이 동작한다.
"""

from datetime import date, timedelta

import pytest

from app.services.schedule_report_service import Issue, ReportDiff, ShiftCell, StoreInfo

pytest.importorskip("weasyprint", reason="WeasyPrint native lib 미설치 호스트")

from app.utils.schedule_report_pdf import (  # noqa: E402
    build_schedule_daily_report_pdf,
)

TODAY = date(2026, 8, 14)
DATES = [TODAY + timedelta(days=i) for i in range(3)]


def _issues(n: int, category: str) -> list[Issue]:
    return [
        Issue(
            key=f"{category}|{i}",
            category=category,
            target_date=DATES[i % 3].isoformat(),
            label=f"{category} issue {i}",
            store_id="s0",
            store_name="Store 0",
            shift_id=None,
            shift_name=None,
            user_id=None,
            user_name=f"Person {i}",
            detail={},
        )
        for i in range(n)
    ]


def _kwargs(diff: ReportDiff, store_count: int = 7):
    stores = [StoreInfo(id=f"s{i}", name=f"Store {i}") for i in range(store_count)]
    cells = [
        ShiftCell(
            store_id=s.id,
            store_name=s.name,
            shift_id=f"{s.id}-{sh}",
            shift_name=f"Shift {sh}",
            shift_sort_order=sh,
            target_date=d,
            staff_count=0 if sh == 2 else 3,
            sv_count=0 if sh in (2, 3) else 1,
        )
        for s in stores
        for sh in range(6)
        for d in DATES
    ]
    return dict(
        org_name="Southern California Restaurant Company",
        sent_date=TODAY,
        target_dates=DATES,
        yesterday=TODAY - timedelta(days=1),
        diff=diff,
        stores=stores,
        cells=cells,
        admin_base_url="https://www.hermesops.site",
    )


BIG_DIFF = ReportDiff(
    new=_issues(3, "sv_gap"),
    ongoing=_issues(60, "shift_understaffed") + _issues(40, "att_over_6h"),
    resolved=_issues(5, "over_6h"),
)


class TestOutput:
    def test_returns_a_real_pdf(self):
        _, data = build_schedule_daily_report_pdf(**_kwargs(BIG_DIFF))
        assert data[:5] == b"%PDF-"
        assert len(data) > 2_000

    def test_filename_carries_org_and_date(self):
        filename, _ = build_schedule_daily_report_pdf(**_kwargs(BIG_DIFF))
        assert filename == "ScheduleReport_SouthernCaliforniaRestaurantCompany_20260814.pdf"

    def test_filename_survives_an_org_name_with_no_alnum(self):
        kwargs = _kwargs(BIG_DIFF)
        kwargs["org_name"] = "///"
        filename, _ = build_schedule_daily_report_pdf(**kwargs)
        assert filename == "ScheduleReport_Org_20260814.pdf"

    def test_paginates_instead_of_overflowing(self):
        pypdf = pytest.importorskip("pypdf")
        _, data = build_schedule_daily_report_pdf(**_kwargs(BIG_DIFF))
        import io

        reader = pypdf.PdfReader(io.BytesIO(data))
        assert len(reader.pages) > 1


class TestContent:
    def _text(self, data: bytes) -> str:
        pypdf = pytest.importorskip("pypdf")
        import io

        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n".join(p.extract_text() for p in reader.pages)

    def _links(self, data: bytes) -> list[str]:
        pypdf = pytest.importorskip("pypdf")
        import io

        out: list[str] = []
        for page in pypdf.PdfReader(io.BytesIO(data)).pages:
            for annot in (page.get("/Annots") or []):
                uri = (annot.get_object().get("/A") or {}).get("/URI")
                if uri:
                    out.append(str(uri))
        return out

    def test_groups_by_store(self):
        """매장 단위 묶음 — 조치가 매장 단위로 일어나므로."""
        _, data = build_schedule_daily_report_pdf(**_kwargs(BIG_DIFF))
        text = self._text(data)
        for i in range(7):
            assert f"Store {i}" in text

    def test_issue_lines_are_clickable(self):
        _, data = build_schedule_daily_report_pdf(**_kwargs(BIG_DIFF))
        links = self._links(data)
        assert links, "PDF 에 클릭 가능한 링크가 하나도 없다"
        assert any("view=daily" in u for u in links)

    def test_store_header_links_to_the_week(self):
        _, data = build_schedule_daily_report_pdf(**_kwargs(BIG_DIFF))
        links = self._links(data)
        weekly = [u for u in links if "view=weekly" in u]
        assert weekly
        # 주는 항상 일요일 시작 (프로젝트 규약). 2026-08-14 는 금요일 → 앵커 08-09.
        assert all("week=2026-08-09" in u for u in weekly)

    def test_links_carry_the_external_marker(self):
        """_ext=1 이 없으면 콘솔이 사용자의 저장 필터로 덮어써서 엉뚱한 화면이 뜬다."""
        _, data = build_schedule_daily_report_pdf(**_kwargs(BIG_DIFF))
        assert all("_ext=1" in u for u in self._links(data))

    def test_lists_open_issues_but_not_resolved_ones(self):
        _, data = build_schedule_daily_report_pdf(**_kwargs(BIG_DIFF))
        text = self._text(data)
        assert "Supervisor coverage gaps" in text
        # resolved 는 개수만 세고 목록에는 넣지 않는다.
        assert "over_6h issue" not in text.replace("att_over_6h issue", "")

    def test_counts_match_the_diff(self):
        _, data = build_schedule_daily_report_pdf(**_kwargs(BIG_DIFF))
        text = self._text(data)
        assert "New 3" in text
        assert "Ongoing 100" in text
        assert "Resolved 5" in text


class TestEdgeCases:
    def test_empty_diff_still_renders(self):
        empty = ReportDiff(new=[], ongoing=[], resolved=[])
        _, data = build_schedule_daily_report_pdf(**_kwargs(empty))
        assert data[:5] == b"%PDF-"

    def test_no_stores_still_renders(self):
        kwargs = _kwargs(ReportDiff(new=[], ongoing=[], resolved=[]), store_count=0)
        _, data = build_schedule_daily_report_pdf(**kwargs)
        assert data[:5] == b"%PDF-"

    def test_unknown_category_is_not_silently_dropped(self):
        """CATEGORY_TITLES 에 없는 새 카테고리도 PDF 에 나와야 한다."""
        diff = ReportDiff(new=_issues(2, "brand_new_category"), ongoing=[], resolved=[])
        _, data = build_schedule_daily_report_pdf(**_kwargs(diff))
        pypdf = pytest.importorskip("pypdf")
        import io

        text = "\n".join(
            p.extract_text() for p in pypdf.PdfReader(io.BytesIO(data)).pages
        )
        assert "brand_new_category" in text


class TestOrphanIssues:
    def test_issue_without_a_store_is_not_dropped(self):
        """store_id 가 없는 이슈도 반드시 나와야 한다 — 매장에 못 붙는다고 사라지면 안 된다."""
        orphan = Issue(
            key="orphan|1",
            category="over_6h",
            target_date=TODAY.isoformat(),
            label="orphan issue with no store",
            store_id=None,
            store_name=None,
            shift_id=None,
            shift_name=None,
            user_id=None,
            user_name="Nobody",
            detail={},
        )
        diff = ReportDiff(new=[orphan], ongoing=[], resolved=[])
        _, data = build_schedule_daily_report_pdf(**_kwargs(diff))

        pypdf = pytest.importorskip("pypdf")
        import io

        text = "\n".join(
            p.extract_text() for p in pypdf.PdfReader(io.BytesIO(data)).pages
        )
        assert "orphan issue with no store" in text
        assert "Not tied to a store" in text
