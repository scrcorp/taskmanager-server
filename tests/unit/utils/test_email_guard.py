"""Unit tests — 비-prod 이메일 라우팅 가드.

이 가드가 지켜야 하는 한 문장: **설정을 깜빡한 비-prod 환경에서는 메일이 나가지
않는다.** 나머지 분기는 그 원칙의 예외를 명시적으로 여는 것뿐이다.
"""

import pytest

from app.utils.email_guard import resolve_email_route


def _route(**kwargs):
    base = dict(
        to="manager@store.com",
        subject="Early clock-in",
        app_env="local",
        redirect_to="",
        send_real=False,
    )
    base.update(kwargs)
    return resolve_email_route(**base)


class TestProduction:
    @pytest.mark.parametrize("env", ["production", "prod", "Production", " PROD "])
    def test_production_sends_as_is(self, env: str) -> None:
        r = _route(app_env=env)
        assert r.send is True
        assert r.to == "manager@store.com"
        assert r.subject == "Early clock-in"  # 제목 안 건드림
        assert r.reason == "production"

    def test_production_ignores_redirect_setting(self) -> None:
        """운영에 실수로 값이 남아 있어도 실제 수신자로 간다."""
        r = _route(app_env="production", redirect_to="me@tigersplus.com")
        assert r.to == "manager@store.com"
        assert r.redirected is False


class TestNonProdDefaultBlock:
    @pytest.mark.parametrize("env", ["local", "staging", "", "worktree"])
    def test_blocked_when_redirect_missing(self, env: str) -> None:
        """이 테스트가 이 파일의 핵심 — 설정 안 했으면 안 나간다."""
        r = _route(app_env=env)
        assert r.send is False
        assert r.to is None
        assert r.reason == "blocked_no_redirect"

    def test_blank_redirect_is_treated_as_missing(self) -> None:
        assert _route(redirect_to="   ").send is False


class TestRedirect:
    def test_redirects_and_tags_original_recipient(self) -> None:
        r = _route(redirect_to="me@tigersplus.com")
        assert r.send is True
        assert r.to == "me@tigersplus.com"
        assert r.subject == "[to: manager@store.com] Early clock-in"
        assert r.redirected is True

    def test_fanout_recipients_are_distinguishable(self) -> None:
        """권한 보유자 전원에게 가는 메일이 한 받은편지함에 모여도 구분돼야 한다."""
        subjects = {
            resolve_email_route(
                to=addr,
                subject="Early clock-in",
                app_env="local",
                redirect_to="me@tigersplus.com",
                send_real=False,
            ).subject
            for addr in ["a@x.com", "b@x.com", "c@x.com"]
        }
        assert len(subjects) == 3

    def test_redirect_trims_whitespace(self) -> None:
        assert _route(redirect_to=" me@tigersplus.com ").to == "me@tigersplus.com"


class TestSendRealOverride:
    def test_explicit_override_sends_to_real_recipient(self) -> None:
        r = _route(send_real=True)
        assert r.send is True
        assert r.to == "manager@store.com"
        assert r.subject == "Early clock-in"
        assert r.reason == "send_real_override"

    def test_override_wins_over_redirect(self) -> None:
        r = _route(send_real=True, redirect_to="me@tigersplus.com")
        assert r.to == "manager@store.com"


class TestNoRecipient:
    @pytest.mark.parametrize("to", ["", "   "])
    def test_empty_recipient_never_sends(self, to: str) -> None:
        r = _route(to=to, redirect_to="me@tigersplus.com")
        assert r.send is False
        assert r.reason == "no_recipient"

    def test_empty_recipient_blocked_in_production_too(self) -> None:
        assert _route(to="", app_env="production").send is False
