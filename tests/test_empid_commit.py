"""EMPID commit_assignments 통합 테스트 + 라우트 인증.

commit 멱등/중복/IDOR/포맷 거부 + empid 라우트 미인증 차단.
"""

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from app.api.backoffice.deps import COOKIE_NAME
from app.config import settings
from app.models.user import User
from app.services import empid_reconcile_service as svc

pytestmark = pytest.mark.asyncio

BASE = "/" + settings.BACKOFFICE_PATH.strip("/")


async def _reset_emp(db, user_ids) -> None:
    await db.execute(update(User).where(User.id.in_(user_ids)).values(employee_no=None))
    await db.commit()


async def test_commit_assign_skip_dup_idor_format(db, seed_organization, test_users) -> None:
    org_id = seed_organization["id"]
    u1 = test_users["teststaff"]["id"]
    u2 = test_users["testsv"]["id"]
    await _reset_emp(db, [u1, u2])

    # 1) NULL → 배정
    r = await svc.commit_assignments(db, org_id, [(u1, "TST-IT-1")])
    assert len(r.assigned) == 1 and not r.skipped and not r.rejected

    # 2) 재실행 → skip (멱등)
    r = await svc.commit_assignments(db, org_id, [(u1, "TST-IT-1")])
    assert len(r.skipped) == 1 and not r.assigned

    # 3) 같은 사번을 다른 유저에 → org-uniqueness 거부
    r = await svc.commit_assignments(db, org_id, [(u2, "TST-IT-1")])
    assert len(r.rejected) == 1 and "already used" in r.rejected[0][1]

    # 4) IDOR — org에 없는 user_id → 거부
    r = await svc.commit_assignments(db, org_id, [(uuid4(), "TST-IT-9")])
    assert len(r.rejected) == 1 and "not found" in r.rejected[0][1]

    # 5) 포맷 위반 (공백/특수문자) → 거부 (u2는 아직 NULL)
    r = await svc.commit_assignments(db, org_id, [(u2, "bad emp!")])
    assert len(r.rejected) == 1 and not r.assigned

    await _reset_emp(db, [u1, u2])  # cleanup


async def test_empid_routes_require_auth(async_client: AsyncClient) -> None:
    # 미인증 → 로그인으로 redirect
    for path in (f"{BASE}/tools/empid", f"{BASE}/tools/empid/commit"):
        method = async_client.get if path.endswith("empid") else async_client.post
        resp = await method(path)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"{BASE}/login"


async def test_empid_wrong_secret_path_404(async_client: AsyncClient) -> None:
    resp = await async_client.get("/wrong-secret/tools/empid")
    assert resp.status_code == 404
