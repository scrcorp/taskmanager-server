"""Unit tests — diff 관측 창 (P0-1).

매 실행마다 today 가 하루 밀리므로 이전 실행과 이번 실행의 관측 창은 다르다.
예전 `diff_issues` 는 창을 따지지 않고 key 집합 뺄셈만 해서, **창 밖으로 나갔을 뿐인
이슈를 "Resolved" 로 보고**했다. 근태 이슈(대상 날짜 = 항상 yesterday)는 매일 전량이
resolved 로 찍혔고, 그 결과 "문제가 사라졌다" 는 신호가 방치된 문제에서 가장 강하게 울렸다.
"""

from datetime import date, timedelta

from app.services.schedule_report_service import (
    Issue,
    diff_issues,
    filter_to_observed_window,
    observed_window,
)

TODAY = date(2026, 8, 14)
YESTERDAY = TODAY - timedelta(days=1)
TARGET_DATES = [TODAY + timedelta(days=i) for i in range(3)]


def _issue(category: str, target_date: date, suffix: str = "") -> Issue:
    return Issue(
        key=f"{category}|{target_date.isoformat()}|{suffix}",
        category=category,
        target_date=target_date.isoformat(),
        label=f"{category} on {target_date.isoformat()}",
        store_id="s0",
        store_name="Store 0",
        shift_id=None,
        shift_name=None,
        user_id=None,
        user_name=None,
        detail={},
    )


class TestObservedWindow:
    def test_covers_target_dates_and_yesterday(self):
        window = observed_window(TARGET_DATES, YESTERDAY)
        assert window == {
            "2026-08-13",
            "2026-08-14",
            "2026-08-15",
            "2026-08-16",
        }

    def test_yesterday_optional(self):
        window = observed_window(TARGET_DATES, None)
        assert "2026-08-13" not in window
        assert len(window) == 3


class TestFilter:
    def test_keeps_issues_inside_the_window(self):
        prev = [_issue("sv_gap", TODAY), _issue("att_over_6h", YESTERDAY)]
        kept = filter_to_observed_window(prev, observed_window(TARGET_DATES, YESTERDAY))
        assert kept == prev

    def test_drops_issues_that_fell_out_of_the_window(self):
        # 어제 실행이 본 08-12 는 오늘 실행의 창(08-13 ~ 08-16)에 없다.
        stale = _issue("shift_understaffed", date(2026, 8, 12))
        fresh = _issue("shift_understaffed", TODAY)
        kept = filter_to_observed_window(
            [stale, fresh], observed_window(TARGET_DATES, YESTERDAY)
        )
        assert kept == [fresh]

    def test_drops_future_dates_beyond_lookahead(self):
        beyond = _issue("shift_understaffed", date(2026, 8, 17))
        kept = filter_to_observed_window(
            [beyond], observed_window(TARGET_DATES, YESTERDAY)
        )
        assert kept == []


class TestRegression:
    """고치기 전 실제로 나던 오보고를 그대로 재현하고, 이제 안 난다는 걸 고정한다."""

    def test_attendance_issues_are_not_reported_resolved_every_day(self):
        # 어제 실행: 08-12 근태 이슈를 스냅샷에 저장.
        prev_snapshot_issues = [_issue("att_over_6h", date(2026, 8, 12), "jane")]
        # 오늘 실행: 근태 대상은 08-13 하나뿐 — 08-12 는 다시 조회조차 하지 않는다.
        current = [_issue("att_over_6h", YESTERDAY, "jane")]

        naive = diff_issues(prev_snapshot_issues, current)
        assert len(naive.resolved) == 1  # ← 예전 동작: 아무것도 안 고쳤는데 resolved

        windowed = diff_issues(
            filter_to_observed_window(
                prev_snapshot_issues, observed_window(TARGET_DATES, YESTERDAY)
            ),
            current,
        )
        assert windowed.resolved == []
        assert len(windowed.new) == 1

    def test_genuinely_fixed_issue_still_reports_resolved(self):
        """창 안에서 진짜로 사라진 이슈는 여전히 resolved 로 잡혀야 한다."""
        prev = [_issue("sv_gap", TODAY, "a"), _issue("sv_gap", TODAY, "b")]
        current = [_issue("sv_gap", TODAY, "a")]

        d = diff_issues(
            filter_to_observed_window(prev, observed_window(TARGET_DATES, YESTERDAY)),
            current,
        )
        assert len(d.resolved) == 1
        assert d.resolved[0].key.endswith("|b")
        assert len(d.ongoing) == 1

    def test_multi_day_outage_does_not_flood_resolved(self):
        """며칠 장애 후 재개해도 이전 스냅샷 전체가 RESOLVED 로 쏟아지지 않는다."""
        old_dates = [date(2026, 8, 5) + timedelta(days=i) for i in range(4)]
        prev = [_issue("shift_understaffed", d) for d in old_dates]
        current = [_issue("shift_understaffed", TODAY)]

        d = diff_issues(
            filter_to_observed_window(prev, observed_window(TARGET_DATES, YESTERDAY)),
            current,
        )
        assert d.resolved == []
        assert len(d.new) == 1
