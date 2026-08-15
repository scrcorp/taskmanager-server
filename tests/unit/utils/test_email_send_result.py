"""Unit tests — send_email 의 반환값이 "실제로 나갔는지" 를 말하는지 (P0-6).

가드는 조용히 return 한다. 그래서 호출부가 예외만 보고 성공을 판단하면
**"보냈다고 기록됐는데 실제로는 안 나간"** 상태가 된다 — 스케줄 일일 보고서에서
실제로 그 일이 있었고(2026-08-14), 응답은 sent=true 인데 아무도 메일을 못 받았다.

여기서 고정하는 성질: 발송되면 True, 가드가 막으면 False.
"""

import pytest

from app.config import settings
from app.utils import email as email_util


@pytest.fixture
def sent(monkeypatch):
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
async def test_blocked_returns_false(sent) -> None:
    result = await email_util.send_email(
        to="boss@store.com", subject="Daily report", html="<p>hi</p>"
    )
    assert result is False
    assert sent == []


@pytest.mark.asyncio
async def test_production_returns_true(monkeypatch, sent) -> None:
    monkeypatch.setattr(settings, "APP_ENV", "production", raising=False)
    result = await email_util.send_email(
        to="boss@store.com", subject="Daily report", html="<p>hi</p>"
    )
    assert result is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_redirected_returns_true(monkeypatch, sent) -> None:
    monkeypatch.setattr(settings, "EMAIL_REDIRECT_TO", "dev@store.com", raising=False)
    result = await email_util.send_email(
        to="boss@store.com", subject="Daily report", html="<p>hi</p>"
    )
    assert result is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_send_real_override_returns_true(monkeypatch, sent) -> None:
    monkeypatch.setattr(settings, "EMAIL_SEND_REAL", True, raising=False)
    result = await email_util.send_email(
        to="boss@store.com", subject="Daily report", html="<p>hi</p>"
    )
    assert result is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_empty_recipient_returns_false(monkeypatch, sent) -> None:
    monkeypatch.setattr(settings, "APP_ENV", "production", raising=False)
    result = await email_util.send_email(to="", subject="x", html="<p>hi</p>")
    assert result is False
    assert sent == []


@pytest.mark.asyncio
async def test_smtp_failure_still_raises(monkeypatch) -> None:
    """예외는 삼키지 않는다 — 호출부가 수신자별로 잡아서 기록해야 한다."""
    monkeypatch.setattr(settings, "APP_ENV", "production", raising=False)

    async def _boom(msg, **kwargs):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(email_util.aiosmtplib, "send", _boom)

    with pytest.raises(RuntimeError):
        await email_util.send_email(to="boss@store.com", subject="x", html="<p>hi</p>")
