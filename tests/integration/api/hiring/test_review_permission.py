"""hiring:write 권한 — GM 까지 리뷰 작성 가능, SV/Staff 불가.

회귀 가드: `hiring:write` 가 PERMISSION_REGISTRY 에 미등록이라 GM 리뷰가 403 이던 버그.
정책 결정(2026-06-01): hiring 은 Owner+GM 까지. SV 는 당분간 hiring 접근 불가.
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.permissions import DEFAULT_ROLE_PERMISSIONS
from app.database import async_session
from app.main import app
from app.models.hiring import Application, ApplicationReview, Candidate
from app.models.permission import Permission, RolePermission
from app.models.user import User
from app.models.user_store import UserStore
from app.utils.password import hash_password

PW_HASH = hash_password("1234")


def _review_url(app_id: UUID) -> str:
    return f"/api/v1/console/hiring/applications/{app_id}/reviews/me"


async def _login(username: str) -> str:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/console/auth/login",
            json={"username": username, "password": "1234"},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ── unit-level 정책 가드 ──
def test_hiring_write_policy():
    """hiring:write 는 owner/gm 만. sv/staff 에는 없어야 한다."""
    assert "hiring:write" in DEFAULT_ROLE_PERMISSIONS["owner"]
    assert "hiring:write" in DEFAULT_ROLE_PERMISSIONS["gm"]
    assert "hiring:write" not in DEFAULT_ROLE_PERMISSIONS["sv"]
    assert "hiring:write" not in DEFAULT_ROLE_PERMISSIONS["staff"]
    # SV 는 hiring 자체를 못 봄 (read 도 없음)
    assert not any(p.startswith("hiring:") for p in DEFAULT_ROLE_PERMISSIONS["sv"])


@pytest_asyncio.fixture
async def gm_can_review(test_store_id: UUID, test_users: dict):
    """testgm 에게 hiring:write 부여(없으면) + store A 매니저 배정 + application 1건.

    startup lifespan 이 테스트에선 안 돌므로 권한을 idempotent 하게 보장한다.
    """
    gm_id: UUID = test_users["testgm"]["id"]
    created: dict = {}
    async with async_session() as db:
        # hiring:write permission 보장
        perm = (
            await db.execute(select(Permission).where(Permission.code == "hiring:write"))
        ).scalar_one_or_none()
        if perm is None:
            perm = Permission(code="hiring:write", resource="hiring", action="write")
            db.add(perm)
            await db.flush()
            created["perm_id"] = perm.id

        # gm role 에 부여 보장
        gm_user = (await db.execute(select(User).where(User.id == gm_id))).scalar_one()
        gm_role_id = gm_user.role_id
        rp = (
            await db.execute(
                select(RolePermission).where(
                    RolePermission.role_id == gm_role_id,
                    RolePermission.permission_id == perm.id,
                )
            )
        ).scalar_one_or_none()
        if rp is None:
            db.add(RolePermission(role_id=gm_role_id, permission_id=perm.id))
            created["rp"] = (gm_role_id, perm.id)

        # store A 매니저 배정
        us = (
            await db.execute(
                select(UserStore).where(
                    UserStore.user_id == gm_id, UserStore.store_id == test_store_id
                )
            )
        ).scalar_one_or_none()
        if us is None:
            db.add(UserStore(user_id=gm_id, store_id=test_store_id, is_manager=True))
            created["us"] = True
        else:
            us.is_manager = True

        # application 1건 (interview 단계 — 리뷰 가능)
        nonce = uuid.uuid4().hex[:8]
        email = f"__hire_rev_{nonce}@test.local"
        cand = Candidate(
            username=f"__hire_rev_{nonce}",
            email=email,
            email_normalized=email.lower(),
            password_hash=PW_HASH,
            full_name="Review Target",
        )
        db.add(cand)
        await db.flush()
        app_row = Application(candidate_id=cand.id, store_id=test_store_id, stage="interview")
        db.add(app_row)
        await db.flush()
        created.update({"app_id": app_row.id, "cand_id": cand.id})
        await db.commit()

    yield {"application_id": created["app_id"]}

    async with async_session() as db:
        await db.execute(
            delete(ApplicationReview).where(ApplicationReview.application_id == created["app_id"])
        )
        await db.execute(delete(Application).where(Application.id == created["app_id"]))
        await db.execute(delete(Candidate).where(Candidate.id == created["cand_id"]))
        if created.get("us"):
            await db.execute(
                delete(UserStore).where(
                    UserStore.user_id == gm_id, UserStore.store_id == test_store_id
                )
            )
        # perm/role_permission 은 다른 테스트도 쓸 수 있으니 남겨둠
        await db.commit()


@pytest.mark.asyncio
async def test_gm_can_write_review(async_client: AsyncClient, gm_can_review):
    """GM 이 자기 리뷰를 작성하면 200 (이전엔 hiring:write 미등록으로 403)."""
    token = await _login("testgm")
    headers = {"Authorization": f"Bearer {token}"}
    resp = await async_client.put(
        _review_url(gm_can_review["application_id"]),
        headers=headers,
        json={"score": 75, "comment": "good fit"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["score"] == 75


@pytest.mark.asyncio
async def test_sv_cannot_write_review(async_client: AsyncClient, gm_can_review):
    """SV 는 hiring 권한이 없어 리뷰 작성 불가 (403)."""
    token = await _login("testsv")
    headers = {"Authorization": f"Bearer {token}"}
    resp = await async_client.put(
        _review_url(gm_can_review["application_id"]),
        headers=headers,
        json={"score": 50},
    )
    assert resp.status_code == 403, resp.text
