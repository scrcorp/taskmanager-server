"""Unit tests — 알림 이메일 템플릿(build_reply_email) + 환경별 헤더 색.

배경: 이슈 신규 등록/상태 변경 알림이 답글 템플릿의 기본 문구를 그대로 타서
"New reply on your issue report" 로 나갔고, 인용 블록에는 본문 대신 제목이 들어가
정작 내용이 메일에 없었다. 그래서 문구를 호출부가 지정할 수 있어야 하고,
지정하지 않으면 **기존 답글 문구가 그대로 유지**되어야 한다.
"""

import pytest

from app.utils.email_templates import brand_header_style, build_reply_email

BASE = dict(
    recipient_name="Test Staff",
    author_name="Joonmyung Yoon",
    context_label="Issue Report",
    context_subtitle="review · high · report review test",
    excerpt="Follow-up needed: nope",
)


class TestDefaultsPreserveReplyWording:
    """기존 호출부(체크리스트/데일리 답글)가 안 깨져야 한다."""

    def test_subject_and_headline_default_to_reply(self):
        subject, html = build_reply_email(**BASE)
        assert subject == "[Reply] Joonmyung Yoon on issue report"
        assert "New reply on your issue report" in html
        assert "left a reply on:" in html


class TestOverrides:
    def test_event_wording_is_overridable(self):
        subject, html = build_reply_email(
            **BASE,
            subject="[Issue] New · report review test",
            headline="New issue report",
            lead="<strong>Joonmyung Yoon</strong> reported a new issue:",
        )
        assert subject == "[Issue] New · report review test"
        assert "New issue report" in html
        assert "reported a new issue:" in html
        # 답글 문구가 섞여 나가면 안 된다
        assert "New reply on your" not in html
        assert "left a reply on:" not in html

    def test_cta_button_renders_with_custom_label(self):
        _, html = build_reply_email(
            **BASE,
            cta_url="https://console.example.com/reports/issues/abc",
            cta_label="Open issue",
        )
        assert 'href="https://console.example.com/reports/issues/abc"' in html
        assert "Open issue" in html

    def test_no_cta_url_means_no_button(self):
        _, html = build_reply_email(**BASE)
        assert "Open issue" not in html
        assert "Open in HTM" not in html


class TestExcerpt:
    def test_body_is_included_and_newlines_survive(self):
        """본문이 실제로 메일에 들어가야 하고, 여러 줄이면 줄바꿈이 살아야 한다."""
        payload = dict(BASE)
        payload["excerpt"] = "Follow-up needed: nope\n\nPlan: keep going"
        _, html = build_reply_email(**payload)
        assert "Follow-up needed: nope" in html
        assert "Plan: keep going" in html
        assert "white-space:pre-wrap" in html

    def test_empty_excerpt_uses_custom_fallback(self):
        payload = dict(BASE)
        payload["excerpt"] = None
        _, html = build_reply_email(**payload, excerpt_fallback="(No description provided)")
        assert "(No description provided)" in html
        assert "Photo or video" not in html

    def test_excerpt_is_html_escaped(self):
        payload = dict(BASE)
        payload["excerpt"] = "<script>alert(1)</script>"
        _, html = build_reply_email(**payload)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestBrandHeaderByEnv:
    """주황/보라는 비-prod 전용 신호다 — prod 메일에 나가면 안 된다."""

    @pytest.mark.parametrize(
        "env,expected_bg,expected_label",
        [
            ("production", "#FBF7F0", ""),
            ("staging", "#6C5CE7", "STG"),
            ("local", "#E4791B", "DEV"),
            ("dev", "#E4791B", "DEV"),
        ],
    )
    def test_header_color_and_badge(self, monkeypatch, env, expected_bg, expected_label):
        from app.config import settings

        monkeypatch.setattr(settings, "APP_ENV", env)
        bg, _fg, label = brand_header_style()
        assert bg == expected_bg
        assert label == expected_label

        _, html = build_reply_email(**BASE)
        assert f"background-color:{expected_bg}" in html
        if expected_label:
            assert f">{expected_label}</span>" in html

    def test_prod_has_no_env_badge(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "APP_ENV", "production")
        _, html = build_reply_email(**BASE)
        assert ">STG</span>" not in html
        assert ">DEV</span>" not in html
