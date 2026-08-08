"""이메일 대소문자 — 저장 정규화가 실제 API 경로에서 지켜지는지.

배경(실제 사고):
    users.email 은 입력 원본이 그대로 저장돼 왔는데, 이메일로 사용자를 찾는
    코드는 전부 입력만 소문자화해서 원본 컬럼과 `==` 비교했다. 그래서 대문자가
    섞인 주소로 만들어진 계정은
      (1) 이메일 중복 체크에 걸리지 않아 같은 주소로 계정이 또 만들어졌고
      (2) 비밀번호 재설정 / 아이디 찾기에서 "계정 없음"이 됐다.
    dev DB 에서 실제로 5개 계정이 이 상태였고, 그중 한 주소는 계정이 2개였다.

여기서 고정하는 계약:
    users.email 에 값을 쓰는 모든 API 경로는 trim + 소문자로 저장한다.
    그 결과 대소문자를 어떻게 입력하든 같은 계정으로 수렴한다.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.models.user import User

pytestmark = pytest.mark.asyncio


def _mixed_case_email() -> tuple[str, str]:
    """(입력값, 기대 저장값). 매 테스트 고유 주소라 시드와 충돌하지 않는다."""
    token = uuid.uuid4().hex[:10]
    return f"QA.Mixed.{token}@Example.COM", f"qa.mixed.{token}@example.com"


@pytest_asyncio.fixture
async def cleanup_users():
    """테스트가 만든 user 를 반드시 지운다 — 이메일은 org 전역에서 겹치면 안 된다."""
    created: list[uuid.UUID] = []
    yield created
    if created:
        async with async_session() as db:
            await db.execute(delete(User).where(User.id.in_(created)))
            await db.commit()


async def _stored_email(user_id: uuid.UUID) -> str | None:
    async with async_session() as db:
        return (
            await db.execute(select(User.email).where(User.id == user_id))
        ).scalar_one()


class TestAdminCreatesUser:
    """POST /api/v1/console/users — 관리자가 직원 생성."""

    async def test_mixed_case_email_is_stored_lowercase(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        seed_roles: dict[str, uuid.UUID],
        cleanup_users: list,
    ) -> None:
        raw, canon = _mixed_case_email()
        resp = await async_client.post(
            "/api/v1/console/users",
            headers=admin_headers,
            json={
                "username": f"qa{uuid.uuid4().hex[:10]}",
                "password": "password123",
                "full_name": "QA Mixed Case",
                "email": raw,
                "role_id": str(seed_roles["staff"]),
            },
        )
        assert resp.status_code == 201, resp.text
        user_id = uuid.UUID(resp.json()["id"])
        cleanup_users.append(user_id)

        assert await _stored_email(user_id) == canon

    async def test_surrounding_whitespace_is_trimmed(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        seed_roles: dict[str, uuid.UUID],
        cleanup_users: list,
    ) -> None:
        raw, canon = _mixed_case_email()
        resp = await async_client.post(
            "/api/v1/console/users",
            headers=admin_headers,
            json={
                "username": f"qa{uuid.uuid4().hex[:10]}",
                "password": "password123",
                "full_name": "QA Whitespace",
                "email": f"   {raw}  ",
                "role_id": str(seed_roles["staff"]),
            },
        )
        assert resp.status_code == 201, resp.text
        user_id = uuid.UUID(resp.json()["id"])
        cleanup_users.append(user_id)

        assert await _stored_email(user_id) == canon


class TestFindUsername:
    """POST /api/v1/auth/find-username — 사고 (2) 재현: 대문자 계정은 못 찾았다."""

    @pytest_asyncio.fixture
    async def user_with_mixed_case_email(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        seed_roles: dict[str, uuid.UUID],
        cleanup_users: list,
    ) -> tuple[str, str, str]:
        """대문자 이메일로 생성된 계정. (원본입력, canonical, username)"""
        raw, canon = _mixed_case_email()
        username = f"qa{uuid.uuid4().hex[:10]}"
        resp = await async_client.post(
            "/api/v1/console/users",
            headers=admin_headers,
            json={
                "username": username,
                "password": "password123",
                "full_name": "QA Find Me",
                "email": raw,
                "role_id": str(seed_roles["staff"]),
            },
        )
        assert resp.status_code == 201, resp.text
        cleanup_users.append(uuid.UUID(resp.json()["id"]))
        return raw, canon, username

    async def test_found_by_original_mixed_case_input(
        self, async_client: AsyncClient, user_with_mixed_case_email: tuple[str, str, str]
    ) -> None:
        """가입할 때 쓴 그대로 입력 — 정규화 전에는 여기서 404 였다."""
        raw, _canon, _username = user_with_mixed_case_email
        resp = await async_client.post(
            "/api/v1/auth/find-username", json={"email": raw}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["masked_username"]

    async def test_found_by_lowercase_input(
        self, async_client: AsyncClient, user_with_mixed_case_email: tuple[str, str, str]
    ) -> None:
        """소문자로 입력해도 같은 계정으로 수렴한다."""
        _raw, canon, _username = user_with_mixed_case_email
        resp = await async_client.post(
            "/api/v1/auth/find-username", json={"email": canon}
        )
        assert resp.status_code == 200, resp.text

    async def test_found_by_uppercase_input(
        self, async_client: AsyncClient, user_with_mixed_case_email: tuple[str, str, str]
    ) -> None:
        """전부 대문자로 입력해도 마찬가지."""
        _raw, canon, _username = user_with_mixed_case_email
        resp = await async_client.post(
            "/api/v1/auth/find-username", json={"email": canon.upper()}
        )
        assert resp.status_code == 200, resp.text


class TestSignupDuplicateDetection:
    """사고 (1) 재현: 대문자 계정이 있어도 같은 주소로 또 가입됐다."""

    @pytest_asyncio.fixture
    async def verified_user_with_mixed_case_email(
        self,
        async_client: AsyncClient,
        admin_headers: dict[str, str],
        seed_roles: dict[str, uuid.UUID],
        cleanup_users: list,
    ) -> tuple[str, str]:
        """이메일 인증까지 끝난 계정. 중복 체크는 verified 계정만 본다."""
        raw, canon = _mixed_case_email()
        resp = await async_client.post(
            "/api/v1/console/users",
            headers=admin_headers,
            json={
                "username": f"qa{uuid.uuid4().hex[:10]}",
                "password": "password123",
                "full_name": "QA Duplicate",
                "email": raw,
                "role_id": str(seed_roles["staff"]),
            },
        )
        assert resp.status_code == 201, resp.text
        user_id = uuid.UUID(resp.json()["id"])
        cleanup_users.append(user_id)

        async with async_session() as db:
            user = (
                await db.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            user.email_verified = True
            await db.commit()
        return raw, canon

    @pytest.mark.parametrize("case", ["raw", "lower", "upper"])
    async def test_duplicate_caught_regardless_of_case(
        self,
        async_client: AsyncClient,
        verified_user_with_mixed_case_email: tuple[str, str],
        case: str,
    ) -> None:
        """어떤 대소문자로 시도해도 중복으로 막힌다 (SMTP 전에 409)."""
        raw, canon = verified_user_with_mixed_case_email
        attempt = {"raw": raw, "lower": canon, "upper": canon.upper()}[case]

        resp = await async_client.post(
            "/api/v1/app/auth/send-verification-code",
            json={"email": attempt, "purpose": "registration"},
        )
        assert resp.status_code == 409, resp.text

    @pytest.mark.parametrize("case", ["raw", "lower", "upper"])
    async def test_check_availability_reports_taken(
        self,
        async_client: AsyncClient,
        verified_user_with_mixed_case_email: tuple[str, str],
        test_store_id: uuid.UUID,
        case: str,
    ) -> None:
        """가입 폼 선체크도 같은 답을 낸다 — 선체크와 최종 판정이 어긋나면 안 된다."""
        from app.core.url_encoding import encode_uuid

        raw, canon = verified_user_with_mixed_case_email
        attempt = {"raw": raw, "lower": canon, "upper": canon.upper()}[case]

        resp = await async_client.post(
            "/api/v1/app/auth/check-availability",
            json={
                "encoded": encode_uuid(test_store_id),
                "username": f"qa{uuid.uuid4().hex[:10]}",
                "email": attempt,
                "mode": "direct",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["email_available"] is False
