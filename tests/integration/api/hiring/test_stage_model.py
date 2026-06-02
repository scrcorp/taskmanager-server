"""단계 모델 (2026-06-01 변경) 가드 — reviewing→screen rename + review 신규.

- 상수: screen/review 포함, reviewing 제외
- patch 전이: screen/interview/review 허용, 옛 'reviewing' 은 400
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete

from app.core.hiring import ACTIVE_STAGES, APPLICATION_STAGES, MANAGER_VISIBLE_STAGES
from app.database import async_session
from app.models.hiring import Application, Candidate
from app.utils.password import hash_password

PW_HASH = hash_password("1234")


def test_stage_constants():
    assert "screen" in APPLICATION_STAGES
    assert "review" in APPLICATION_STAGES
    assert "reviewing" not in APPLICATION_STAGES
    # 활성/표시 단계에도 반영
    assert "screen" in ACTIVE_STAGES and "review" in ACTIVE_STAGES
    assert "reviewing" not in ACTIVE_STAGES
    assert "screen" in MANAGER_VISIBLE_STAGES and "review" in MANAGER_VISIBLE_STAGES
    # 흐름 순서 보존: new < screen < interview < review
    idx = {s: i for i, s in enumerate(APPLICATION_STAGES)}
    assert idx["new"] < idx["screen"] < idx["interview"] < idx["review"] < idx["hired"]


@pytest_asyncio.fixture
async def app_in_screen(test_store_id: UUID):
    nonce = uuid.uuid4().hex[:8]
    email = f"__hire_stage_{nonce}@test.local"
    async with async_session() as db:
        cand = Candidate(
            username=f"__hire_stage_{nonce}",
            email=email,
            email_normalized=email.lower(),
            password_hash=PW_HASH,
            full_name="Stage Tester",
        )
        db.add(cand)
        await db.flush()
        app_row = Application(candidate_id=cand.id, store_id=test_store_id, stage="screen")
        db.add(app_row)
        await db.flush()
        ids = (app_row.id, cand.id)
        await db.commit()
    yield {"application_id": ids[0]}
    async with async_session() as db:
        await db.execute(delete(Application).where(Application.id == ids[0]))
        await db.execute(delete(Candidate).where(Candidate.id == ids[1]))
        await db.commit()


def _patch_url(app_id: UUID) -> str:
    return f"/api/v1/console/hiring/applications/{app_id}"


@pytest.mark.asyncio
async def test_forward_transitions(async_client: AsyncClient, admin_headers, app_in_screen):
    """screen → interview → review 전이가 200."""
    url = _patch_url(app_in_screen["application_id"])
    for stage in ("interview", "review"):
        resp = await async_client.patch(url, headers=admin_headers, json={"stage": stage})
        assert resp.status_code == 200, resp.text
        assert resp.json()["stage"] == stage


@pytest.mark.asyncio
async def test_old_reviewing_rejected(async_client: AsyncClient, admin_headers, app_in_screen):
    """옛 'reviewing' 단계로의 patch 는 400 (모델에서 사라짐)."""
    resp = await async_client.patch(
        _patch_url(app_in_screen["application_id"]),
        headers=admin_headers,
        json={"stage": "reviewing"},
    )
    assert resp.status_code == 400, resp.text
