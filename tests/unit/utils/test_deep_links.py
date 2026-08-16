"""Unit tests — 알림 이메일 CTA 딥링크.

이 유틸이 지켜야 하는 한 문장: **수신자가 실제로 열 수 있는 화면으로 보낸다.**
콘솔은 SV 미만의 로그인을 막으므로, staff 에게 콘솔 링크를 주면 로그인 벽에 막힌다.
"""

import pytest

from app.core.permissions import (
    GM_PRIORITY,
    OWNER_PRIORITY,
    STAFF_PRIORITY,
    SV_PRIORITY,
)
from app.utils.deep_links import build_cta_url

CONSOLE = "https://console.example.com"
STAFF_APP = "https://app.example.com"


@pytest.fixture(autouse=True)
def _urls(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ADMIN_BASE_URL", CONSOLE)
    monkeypatch.setattr(settings, "STAFF_APP_BASE_URL", STAFF_APP)


class TestRecipientRouting:
    @pytest.mark.parametrize(
        "priority", [OWNER_PRIORITY, GM_PRIORITY, SV_PRIORITY]
    )
    def test_sv_plus_gets_console_link(self, priority):
        assert build_cta_url("issue_report", "abc", priority) == (
            f"{CONSOLE}/reports/issues/abc"
        )

    def test_staff_gets_app_link(self):
        assert build_cta_url("issue_report", "abc", STAFF_PRIORITY) == (
            f"{STAFF_APP}/issue-reports/abc"
        )

    def test_unknown_priority_falls_back_to_console(self):
        """priority 를 모르면 기존 동작(콘솔)을 유지한다 — 조용히 링크를 잃지 않는다."""
        assert build_cta_url("issue_report", "abc", None) == (
            f"{CONSOLE}/reports/issues/abc"
        )


class TestKinds:
    @pytest.mark.parametrize(
        "kind,console_path,app_path",
        [
            ("issue_report", "/reports/issues/x", "/issue-reports/x"),
            ("daily_report", "/daily-reports/x", "/daily-reports/x"),
            ("checklist_instance", "/checklists/instances/x", "/work/x"),
        ],
    )
    def test_each_kind_maps_to_both_surfaces(self, kind, console_path, app_path):
        assert build_cta_url(kind, "x", GM_PRIORITY) == CONSOLE + console_path
        assert build_cta_url(kind, "x", STAFF_PRIORITY) == STAFF_APP + app_path

    def test_unknown_kind_returns_none(self):
        """모르는 종류면 링크를 만들지 않는다 — 깨진 URL 버튼보다 버튼 없는 게 낫다."""
        assert build_cta_url("mystery", "x", GM_PRIORITY) is None


class TestMissingConfig:
    def test_staff_falls_back_to_console_when_app_url_unset(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "STAFF_APP_BASE_URL", "")
        assert build_cta_url("issue_report", "abc", STAFF_PRIORITY) == (
            f"{CONSOLE}/reports/issues/abc"
        )

    def test_returns_none_when_console_url_unset(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "ADMIN_BASE_URL", "")
        assert build_cta_url("issue_report", "abc", GM_PRIORITY) is None

    def test_trailing_slash_does_not_double_up(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "ADMIN_BASE_URL", CONSOLE + "/")
        assert build_cta_url("issue_report", "abc", GM_PRIORITY) == (
            f"{CONSOLE}/reports/issues/abc"
        )
