"""Unit tests — 스냅샷 재발송 재료(payload) 왕복.

재발송이 "다시 만들기" 가 되면 안 된다. 리포트 내용은 실행 시각에 의존하므로
(today 가 밀리고 그 사이 스케줄이 바뀐다) 7시에 나갔어야 할 리포트를 9시에 재생성하면
그건 다른 문서다. 그래서 만든 시점의 재료를 저장하고, 재발송은 그걸로 재현한다.

여기서 고정하는 성질: payload → render kwargs 왕복이 무손실이어야 한다.
하나라도 빠지면 재발송본이 원본과 달라지는데, 그건 조용히 일어난다.
"""

from datetime import date, timedelta

from app.services.schedule_report_service import (
    Issue,
    ReportDiff,
    ShiftCell,
    StoreInfo,
    build_render_payload,
    render_kwargs_from_payload,
)

TODAY = date(2026, 8, 14)
DATES = [TODAY + timedelta(days=i) for i in range(3)]
YESTERDAY = TODAY - timedelta(days=1)


def _issue(i: int, category: str = "sv_gap") -> Issue:
    return Issue(
        key=f"{category}|{i}",
        category=category,
        target_date=DATES[i % 3].isoformat(),
        label=f"{category} issue {i}",
        store_id="store-1",
        store_name="Store 1",
        shift_id=None,
        shift_name=None,
        user_id=None,
        user_name=f"Person {i}",
        detail={"minutes": 60 * i},
    )


STORES = [StoreInfo(id="store-1", name="Store 1"), StoreInfo(id="store-2", name="Store 2")]
CELLS = [
    ShiftCell(
        store_id="store-1",
        store_name="Store 1",
        shift_id="shift-a",
        shift_name="Open",
        shift_sort_order=0,
        target_date=DATES[0],
        staff_count=3,
        sv_count=1,
    ),
    ShiftCell(
        store_id="store-2",
        store_name="Store 2",
        shift_id="shift-b",
        shift_name="Close",
        shift_sort_order=1,
        target_date=DATES[2],
        staff_count=0,
        sv_count=0,
    ),
]
DIFF = ReportDiff(new=[_issue(0)], ongoing=[_issue(1), _issue(2)], resolved=[_issue(3)])


def _payload():
    return build_render_payload(
        org_name="Southern California Restaurant Company",
        sent_date=TODAY,
        target_dates=DATES,
        yesterday=YESTERDAY,
        diff=DIFF,
        stores=STORES,
        cells=CELLS,
    )


class TestRoundTrip:
    def test_scalars_survive(self):
        kw = render_kwargs_from_payload(_payload(), "https://console.example.com")
        assert kw["org_name"] == "Southern California Restaurant Company"
        assert kw["sent_date"] == TODAY
        assert kw["target_dates"] == DATES
        assert kw["yesterday"] == YESTERDAY

    def test_stores_survive(self):
        kw = render_kwargs_from_payload(_payload(), "https://console.example.com")
        assert kw["stores"] == STORES

    def test_cells_survive_including_dates(self):
        """date 는 JSON 에 없는 타입이라 여기가 가장 깨지기 쉽다."""
        kw = render_kwargs_from_payload(_payload(), "https://console.example.com")
        assert kw["cells"] == CELLS
        assert kw["cells"][0].target_date == DATES[0]
        assert isinstance(kw["cells"][0].target_date, date)

    def test_diff_survives_all_three_buckets(self):
        kw = render_kwargs_from_payload(_payload(), "https://console.example.com")
        diff = kw["diff"]
        assert diff.new == DIFF.new
        assert diff.ongoing == DIFF.ongoing
        assert diff.resolved == DIFF.resolved

    def test_issue_detail_dict_survives(self):
        kw = render_kwargs_from_payload(_payload(), "https://console.example.com")
        assert kw["diff"].ongoing[0].detail == {"minutes": 60}


class TestAdminBaseUrl:
    def test_taken_from_current_settings_not_the_snapshot(self):
        """콘솔 주소가 바뀌면 과거 리포트의 링크도 새 주소를 가리켜야 한다.

        payload 에 굳혀두면 주소 변경 후 재발송한 메일의 링크가 전부 죽는다.
        """
        payload = _payload()
        assert "admin_base_url" not in payload

        kw = render_kwargs_from_payload(payload, "https://new-console.example.com")
        assert kw["admin_base_url"] == "https://new-console.example.com"


class TestJsonSafety:
    def test_payload_is_json_serializable(self):
        """JSONB 컬럼에 들어가므로 순수 JSON 타입만 있어야 한다."""
        import json

        json.dumps(_payload())

    def test_empty_diff_and_no_yesterday(self):
        payload = build_render_payload(
            org_name="Org",
            sent_date=TODAY,
            target_dates=DATES,
            yesterday=None,
            diff=ReportDiff(new=[], ongoing=[], resolved=[]),
            stores=[],
            cells=[],
        )
        kw = render_kwargs_from_payload(payload, "https://console.example.com")
        assert kw["yesterday"] is None
        assert kw["stores"] == []
        assert kw["cells"] == []
        assert kw["diff"].new == []
