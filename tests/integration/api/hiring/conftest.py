"""Hiring 테스트 공통 — 실제 이메일 발송 차단 + 인터뷰 슬롯 클린 상태.

- worktree .env 에 실 SMTP(Brevo) 자격이 있어, 인터뷰 초대/확정 메일이 실제로 나가는 걸 막는다.
- interview_slots 는 org 통합이라 테스트 간 누적되면 카운트 단언이 깨진다 → 각 테스트 전후 purge.
"""

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.database import async_session
from app.models.interview import InterviewSlot, InterviewSlotPreference


@pytest.fixture(autouse=True)
def _no_email(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.interview_email_service.send_email", _noop)


@pytest_asyncio.fixture(autouse=True)
async def _clean_interview_slots():
    async def purge():
        async with async_session() as db:
            await db.execute(delete(InterviewSlotPreference))
            await db.execute(delete(InterviewSlot))
            await db.commit()

    await purge()
    yield
    await purge()
