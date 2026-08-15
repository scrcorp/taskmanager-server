"""Integration tests — POST /console/schedule-report/run.

여기서 고정하는 성질 셋:
  1. 수동 실행은 **절대 스냅샷(diff 베이스라인)을 쓰지 않는다** (P0-7).
     예전엔 오너가 오전에 한 번 눌러보면 그날 15:00 리포트의 NEW 배지가 전부 사라졌다.
  2. 발송 시 상세 PDF 가 첨부되고, 본문은 축약본이다 (P0-5).
  3. 가드에 막히면 sent=false 로 정직하게 보고한다 (P0-6).
     예전엔 가드가 조용히 막아도 sent=true 였다.
"""

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models.schedule_report import ScheduleReportSnapshot
from app.utils import email as email_util

ENDPOINT = "/api/v1/console/schedule-report/run"


@pytest.fixture
def captured(monkeypatch):
    """aiosmtplib.send 가로채기 + 실제 발송 경로가 돌도록 가드 통과."""
    messages: list = []

    async def _fake_send(msg, **kwargs):
        messages.append(msg)

    monkeypatch.setattr(email_util.aiosmtplib, "send", _fake_send)
    monkeypatch.setattr(settings, "APP_ENV", "production", raising=False)
    return messages


async def _snapshot_count(db) -> int:
    return await db.scalar(
        select(func.count()).select_from(ScheduleReportSnapshot)
    )


class TestPermissions:
    @pytest.mark.asyncio
    async def test_requires_auth(self, async_client):
        resp = await async_client.post(ENDPOINT)
        assert resp.status_code in (401, 403)


class TestBaselineIsNeverWritten:
    @pytest.mark.asyncio
    async def test_dry_run_saves_nothing(self, async_client, admin_headers, db):
        before = await _snapshot_count(db)
        resp = await async_client.post(
            ENDPOINT, params={"dry_run": True}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["snapshot_saved"] is False
        assert await _snapshot_count(db) == before

    @pytest.mark.asyncio
    async def test_real_send_saves_nothing_either(
        self, async_client, admin_headers, db, captured
    ):
        """이게 P0-7 의 핵심 — 발송했어도 베이스라인은 크론만 쓴다."""
        before = await _snapshot_count(db)
        resp = await async_client.post(
            ENDPOINT, params={"to": "owner@example.com"}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["snapshot_saved"] is False
        assert await _snapshot_count(db) == before


class TestDryRun:
    @pytest.mark.asyncio
    async def test_sends_nothing_and_returns_html(
        self, async_client, admin_headers, captured
    ):
        resp = await async_client.post(
            ENDPOINT, params={"dry_run": True}, headers=admin_headers
        )
        body = resp.json()
        assert resp.status_code == 200
        assert body["sent"] is False
        assert body["recipients"] == []
        assert body["html"]
        assert captured == []

    @pytest.mark.asyncio
    async def test_does_not_render_a_pdf(self, async_client, admin_headers, captured):
        """미리보기에서 PDF 를 굽지 않는다 — 수신자가 없으면 렌더 자체를 건너뛴다."""
        resp = await async_client.post(
            ENDPOINT, params={"dry_run": True}, headers=admin_headers
        )
        assert resp.json()["pdf_attached"] is False


class TestRealSend:
    @pytest.mark.asyncio
    async def test_attaches_the_pdf(self, async_client, admin_headers, captured):
        pytest.importorskip("weasyprint", reason="WeasyPrint native lib 미설치 호스트")
        resp = await async_client.post(
            ENDPOINT, params={"to": "owner@example.com"}, headers=admin_headers
        )
        body = resp.json()
        assert resp.status_code == 200, resp.text
        assert body["sent"] is True
        assert body["delivered"] == ["owner@example.com"]
        assert body["failed"] == []
        assert body["pdf_attached"] is True

        assert len(captured) == 1
        filenames = [
            part.get_filename()
            for part in captured[0].walk()
            if part.get_filename()
        ]
        assert any(f.startswith("ScheduleReport_") and f.endswith(".pdf") for f in filenames)

    @pytest.mark.asyncio
    async def test_body_is_the_compact_variant(
        self, async_client, admin_headers, captured
    ):
        pytest.importorskip("weasyprint", reason="WeasyPrint native lib 미설치 호스트")
        await async_client.post(
            ENDPOINT, params={"to": "owner@example.com"}, headers=admin_headers
        )
        html_parts = [
            p.get_payload(decode=True).decode()
            for p in captured[0].walk()
            if p.get_content_type() == "text/html"
        ]
        assert html_parts
        html = html_parts[0]
        assert "attached PDF" in html
        # 무거운 섹션은 본문에서 빠져 있어야 한다 (Gmail 클리핑 방지)
        assert "Staffing by Shift" not in html

    @pytest.mark.asyncio
    async def test_multiple_recipients_are_independent(
        self, async_client, admin_headers, captured
    ):
        resp = await async_client.post(
            ENDPOINT,
            params={"to": "a@example.com,b@example.com"},
            headers=admin_headers,
        )
        body = resp.json()
        assert sorted(body["delivered"]) == ["a@example.com", "b@example.com"]
        assert len(captured) == 2


class TestGuardBlocked:
    @pytest.mark.asyncio
    async def test_reports_not_sent_when_guard_blocks(
        self, async_client, admin_headers, monkeypatch
    ):
        """가드가 막으면 sent=false. 예전엔 여기서 true 가 나와서 원인 추적이 불가능했다."""
        monkeypatch.setattr(settings, "APP_ENV", "local", raising=False)
        monkeypatch.setattr(settings, "EMAIL_REDIRECT_TO", "", raising=False)
        monkeypatch.setattr(settings, "EMAIL_SEND_REAL", False, raising=False)

        resp = await async_client.post(
            ENDPOINT, params={"to": "owner@example.com"}, headers=admin_headers
        )
        body = resp.json()
        assert resp.status_code == 200, resp.text
        assert body["sent"] is False
        assert body["delivered"] == []
        assert body["failed"] == [
            {"to": "owner@example.com", "reason": "blocked_by_email_guard"}
        ]


RESEND = "/api/v1/console/schedule-report/resend"


class TestResend:
    """/resend 는 **마지막 저장본**을 보낸다 — 새로 만들지 않는다."""

    @pytest.mark.asyncio
    async def test_requires_owner(self, async_client):
        resp = await async_client.post(RESEND)
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_reports_when_there_is_nothing_to_resend(
        self, async_client, admin_headers, db, captured
    ):
        """스냅샷이 없으면 조용히 성공한 척하지 않는다."""
        from sqlalchemy import delete

        await db.execute(delete(ScheduleReportSnapshot))
        await db.commit()

        resp = await async_client.post(
            RESEND, params={"to": "owner@example.com"}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["sent"] is False
        assert body["reason"] == "no_snapshot"
        assert captured == []

    @pytest.mark.asyncio
    async def test_resends_the_saved_report(
        self, async_client, admin_headers, db, captured, test_users
    ):
        """크론이 저장한 스냅샷을 그대로 재발송."""
        pytest.importorskip("weasyprint", reason="WeasyPrint native lib 미설치 호스트")
        from sqlalchemy import delete, select

        from app.models.user import User
        from app.services.schedule_report_service import generate_and_send_report

        await db.execute(delete(ScheduleReportSnapshot))
        await db.commit()

        org_id = (
            await db.execute(select(User).where(User.username == "testadmin"))
        ).scalar_one().organization_id

        # 크론과 동일하게 스냅샷을 남기는 실행 (발송은 자기 자신에게만)
        await generate_and_send_report(
            db, org_id, save_snapshot=True, override_recipients=["cron@example.com"]
        )
        captured.clear()

        resp = await async_client.post(
            RESEND, params={"to": "owner@example.com"}, headers=admin_headers
        )
        body = resp.json()
        assert resp.status_code == 200, resp.text
        assert body["sent"] is True
        assert body["delivered"] == ["owner@example.com"]
        assert body["pdf_attached"] is True
        assert body["generated_at"]
        assert len(captured) == 1

    @pytest.mark.asyncio
    async def test_never_writes_a_snapshot(
        self, async_client, admin_headers, db, captured, test_users
    ):
        """재발송이 기준선을 움직이면 다음 리포트의 NEW 가 사라진다."""
        from sqlalchemy import func, select

        from app.models.user import User
        from app.services.schedule_report_service import generate_and_send_report

        org_id = (
            await db.execute(select(User).where(User.username == "testadmin"))
        ).scalar_one().organization_id
        await generate_and_send_report(
            db, org_id, save_snapshot=True, override_recipients=["cron@example.com"]
        )

        before = await db.scalar(
            select(func.count()).select_from(ScheduleReportSnapshot)
        )
        await async_client.post(
            RESEND, params={"to": "owner@example.com"}, headers=admin_headers
        )
        after = await db.scalar(
            select(func.count()).select_from(ScheduleReportSnapshot)
        )
        assert after == before
