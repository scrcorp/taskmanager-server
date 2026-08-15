"""Unit tests — 이메일 본문 축약 스위치 (P0-5, Gmail 클리핑 회귀 테스트).

본문은 매장 수에 비례해 무한히 자랐고 Gmail 은 약 102KB 에서 잘라먹는다.
2026-08-14 prod 실측 본문이 180KB 였다. 여기서 고정하는 성질:

  - `full=False` 는 무거운 4개 섹션을 전부 들어낸다
  - 그래도 요약과 "신규 이슈" 는 남는다 (수신자가 본문만 보고도 판단할 수 있어야 한다)
  - `full=True`(기본값)는 기존 출력 그대로 — `GET /preview` 무퇴행
"""

import base64
from datetime import date, timedelta

from app.services.schedule_report_service import Issue, ReportDiff, ShiftCell, StoreInfo
from app.utils.email_templates import build_schedule_daily_report_email

TODAY = date(2026, 8, 14)
DATES = [TODAY + timedelta(days=i) for i in range(3)]

SECTION_TITLES = [
    "Staffing by Shift",
    "Supervisor Coverage",
    "Overtime — Work over 6 hours",
    "No Break with 8h or more",
]


def _issues(n: int, category: str) -> list[Issue]:
    return [
        Issue(
            key=f"{category}|{i}",
            category=category,
            target_date=DATES[i % 3].isoformat(),
            label=f"{category} issue {i} — Jane Doe (Store 0): 8.5h without break",
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


def _fixture(store_count: int = 7, shift_count: int = 6):
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
        for sh in range(shift_count)
        for d in DATES
    ]
    return stores, cells


def _kwargs(diff: ReportDiff):
    stores, cells = _fixture()
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


class TestFullModeUnchanged:
    def test_default_is_full(self):
        _, html_default = build_schedule_daily_report_email(**_kwargs(BIG_DIFF))
        _, html_full = build_schedule_daily_report_email(**_kwargs(BIG_DIFF), full=True)
        assert html_default == html_full

    def test_full_keeps_every_section(self):
        _, html = build_schedule_daily_report_email(**_kwargs(BIG_DIFF), full=True)
        for title in SECTION_TITLES:
            assert title in html


class TestCompactMode:
    def test_drops_all_heavy_sections(self):
        _, html = build_schedule_daily_report_email(**_kwargs(BIG_DIFF), full=False)
        for title in SECTION_TITLES:
            assert title not in html

    def test_keeps_summary_and_shell(self):
        _, html = build_schedule_daily_report_email(**_kwargs(BIG_DIFF), full=False)
        assert "DAILY SCHEDULE REPORT" in html
        assert "Southern California Restaurant Company" in html
        assert "Open Schedule Console" in html

    def test_points_at_the_attachment(self):
        _, html = build_schedule_daily_report_email(**_kwargs(BIG_DIFF), full=False)
        assert "attached PDF" in html

    def test_lists_new_issues_and_counts_the_rest(self):
        many_new = ReportDiff(new=_issues(25, "sv_gap"), ongoing=[], resolved=[])
        _, html = build_schedule_daily_report_email(**_kwargs(many_new), full=False)
        assert html.count("NEW</span>") == 10  # 상위 10건만 인라인
        assert "+ 15 more" in html

    def test_no_new_issues_says_so(self):
        none_new = ReportDiff(new=[], ongoing=_issues(4, "sv_gap"), resolved=[])
        _, html = build_schedule_daily_report_email(**_kwargs(none_new), full=False)
        assert "No new issues since the last report." in html

    def test_subject_is_identical_in_both_modes(self):
        sub_full, _ = build_schedule_daily_report_email(**_kwargs(BIG_DIFF), full=True)
        sub_compact, _ = build_schedule_daily_report_email(**_kwargs(BIG_DIFF), full=False)
        assert sub_full == sub_compact


class TestGmailClipping:
    """이게 이 파일의 존재 이유다 — 축약본은 절대 클리핑 한계에 닿지 않아야 한다."""

    GMAIL_CLIP_BYTES = 102_000

    def _transfer_size(self, html: str) -> int:
        # 본문은 MIMEText(..., "html") 로 base64 인코딩되어 나간다(약 1.35배).
        return len(base64.b64encode(html.encode()))

    def test_full_body_would_be_clipped(self):
        """전제 확인 — 고치기 전 동작이 실제로 한계를 넘는다."""
        _, html = build_schedule_daily_report_email(**_kwargs(BIG_DIFF), full=True)
        assert self._transfer_size(html) > self.GMAIL_CLIP_BYTES

    def test_compact_body_is_far_below_the_limit(self):
        _, html = build_schedule_daily_report_email(**_kwargs(BIG_DIFF), full=False)
        assert self._transfer_size(html) < 20_000

    def test_compact_stays_small_as_stores_grow(self):
        """매장이 늘어도 축약본은 자라지 않는다 — 본문이 매장 수와 무관해야 한다."""
        stores, cells = _fixture(store_count=40, shift_count=8)
        kwargs = _kwargs(BIG_DIFF)
        kwargs["stores"], kwargs["cells"] = stores, cells
        _, html = build_schedule_daily_report_email(**kwargs, full=False)
        assert self._transfer_size(html) < 20_000
