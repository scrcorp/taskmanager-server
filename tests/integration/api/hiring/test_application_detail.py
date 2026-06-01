"""GET /console/hiring/applications/{id} — 상세 엔드포인트 테스트.

회귀 가드: 리뷰어가 있는 application 상세 조회 시, reviewer.role 를 async 컨텍스트
밖에서 lazy load 하면 MissingGreenlet → 503. selectinload 로 eager load 되어야 200.
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.database import async_session
from app.main import app
from app.models.hiring import Application, ApplicationReview, Candidate
from app.utils.password import hash_password

PW_HASH = hash_password("1234")


def _detail_url(app_id: UUID) -> str:
    return f"/api/v1/console/hiring/applications/{app_id}"


@pytest_asyncio.fixture
async def app_with_review(test_store_id: UUID, test_users: dict):
    """store A 에 application 1건 + testgm(역할 있음) 의 review 1건. 테스트 후 정리."""
    gm_id: UUID = test_users["testgm"]["id"]
    nonce = uuid.uuid4().hex[:8]
    email = f"__hire_detail_{nonce}@test.local"
    async with async_session() as db:
        cand = Candidate(
            username=f"__hire_detail_{nonce}",
            email=email,
            email_normalized=email.lower(),
            password_hash=PW_HASH,
            full_name="Detail Tester",
        )
        db.add(cand)
        await db.flush()
        app_row = Application(candidate_id=cand.id, store_id=test_store_id, stage="interview")
        db.add(app_row)
        await db.flush()
        review = ApplicationReview(
            application_id=app_row.id, reviewer_id=gm_id, score=80, comment="solid"
        )
        db.add(review)
        await db.commit()
        ids = (app_row.id, cand.id, review.id)

    yield {"application_id": ids[0]}

    async with async_session() as db:
        await db.execute(delete(ApplicationReview).where(ApplicationReview.id == ids[2]))
        await db.execute(delete(Application).where(Application.id == ids[0]))
        await db.execute(delete(Candidate).where(Candidate.id == ids[1]))
        await db.commit()


@pytest.mark.asyncio
async def test_detail_with_reviewer_role(async_client: AsyncClient, admin_headers, app_with_review):
    """리뷰어 role priority 가 포함되고 200 으로 반환된다 (MissingGreenlet 회귀 가드)."""
    resp = await async_client.get(_detail_url(app_with_review["application_id"]), headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"] is not None  # 폼 스냅샷 포함
    assert len(body["reviews"]) == 1
    review = body["reviews"][0]
    assert review["score"] == 80
    # 핵심: reviewer.role 가 eager load 되어 priority 가 채워짐
    assert review["reviewer_role_priority"] is not None
