"""Unit tests — send_email 이 가드 결정을 실제로 따르는지.

email_guard 의 순수 로직은 test_email_guard.py 가 덮는다. 여기서는 **SMTP 호출이
정말 일어나지 않는지**를 본다 — 결정만 맞고 배선이 빠지면 메일은 그대로 나간다.
"""

import pytest

from app.config import settings
from app.utils import email as email_util


@pytest.fixture
def sent(monkeypatch):
    """aiosmtplib.send 를 가로채 실제 발송 대신 기록만."""
    calls: list = []

    async def _fake_send(msg, **kwargs):
        calls.append(msg)

    monkeypatch.setattr(email_util.aiosmtplib, "send", _fake_send)
    return calls


@pytest.fixture(autouse=True)
def _base_env(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "local", raising=False)
    monkeypatch.setattr(settings, "EMAIL_REDIRECT_TO", "", raising=False)
    monkeypatch.setattr(settings, "EMAIL_SEND_REAL", False, raising=False)


@pytest.mark.asyncio
async def test_blocked_does_not_touch_smtp(sent) -> None:
    await email_util.send_email(
        to="manager@store.com", subject="Early clock-in", html="<p>hi</p>"
    )
    assert sent == [], "비-prod + redirect 미설정인데 SMTP 를 호출했다"


@pytest.mark.asyncio
async def test_redirected_message_headers(monkeypatch, sent) -> None:
    monkeypatch.setattr(settings, "EMAIL_REDIRECT_TO", "me@tigersplus.com")
    await email_util.send_email(
        to="manager@store.com", subject="Early clock-in", html="<p>hi</p>"
    )
    assert len(sent) == 1
    msg = sent[0]
    assert msg["To"] == "me@tigersplus.com"
    assert msg["Subject"] == "[to: manager@store.com] Early clock-in"


@pytest.mark.asyncio
async def test_production_sends_to_real_recipient(monkeypatch, sent) -> None:
    monkeypatch.setattr(settings, "APP_ENV", "production")
    await email_util.send_email(
        to="manager@store.com", subject="Early clock-in", html="<p>hi</p>"
    )
    assert len(sent) == 1
    assert sent[0]["To"] == "manager@store.com"
    assert sent[0]["Subject"] == "Early clock-in"


@pytest.mark.asyncio
async def test_blocked_email_is_logged(caplog) -> None:
    """조용한 실패 금지 — 왜 안 나갔는지 로그에 남아야 한다."""
    with caplog.at_level("WARNING"):
        await email_util.send_email(
            to="manager@store.com", subject="Early clock-in", html="<p>hi</p>"
        )
    assert "EMAIL_REDIRECT_TO" in caplog.text
    assert "manager@store.com" in caplog.text
