"""공개 가입 폼 선(先)체크 — POST /api/v1/app/auth/check-availability.

가입 폼이 계정 정보 단계를 넘어가기 전에 아이디/이메일 중복을 미리 알려주는
엔드포인트. 판정 규칙이 실제 생성 경로와 어긋나면 "폼은 통과했는데 마지막에
409" 또는 "쓸 수 있는 아이디인데 막힘"이 되므로 여기서 규칙을 고정한다.

  - mode="direct" → /app/auth/direct-signup 과 같은 규칙(username 전역 unique)
  - mode="join"   → /app/applications/start 와 같은 규칙.
                    아이디+이메일이 **둘 다** 같은 기존 지원자는 중복이 아니라
                    "이어서 진행"(resumable)이므로 막지 않는다.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.core.url_encoding import encode_uuid
from app.database import async_session
from app.models.hiring import Candidate
from app.utils.password import hash_password

ENDPOINT = "/api/v1/app/auth/check-availability"


def _unique(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


@pytest_asyncio.fixture
async def encoded_store(test_store_id) -> str:
    """가입 링크의 매장 식별자."""
    return encode_uuid(test_store_id)


@pytest_asyncio.fixture
async def make_candidate():
    """테스트용 지원자 생성 — 만든 행은 테스트 후 반드시 지운다.

    지원자 생성 API(/applications/start)는 이메일 인증 토큰을 요구해 선체크
    규칙만 검증하기엔 과하다. 여기서는 조회 규칙만 보므로 최소 행만 만든다.
    """
    created: list[uuid.UUID] = []

    async def _make(username: str, email: str) -> Candidate:
        async with async_session() as db:
            cand = Candidate(
                username=username,
                email=email,
                email_normalized=email.strip().lower(),
                password_hash=hash_password("password123"),
                email_verified=True,
                full_name="QA Candidate",
                preferred_language="en",
            )
            db.add(cand)
            await db.commit()
            await db.refresh(cand)
            created.append(cand.id)
            return cand

    yield _make

    async with async_session() as db:
        if created:
            await db.execute(delete(Candidate).where(Candidate.id.in_(created)))
            await db.commit()


async def _check(
    client: AsyncClient, encoded: str, username: str, email: str, mode: str
) -> dict:
    res = await client.post(
        ENDPOINT,
        json={
            "encoded": encoded,
            "username": username,
            "email": email,
            "mode": mode,
        },
    )
    assert res.status_code == 200, res.text
    return res.json()


class TestDirectMode:
    """mode="direct" — /auth/direct-signup 과 같은 규칙."""

    @pytest.mark.asyncio
    async def test_fresh_username_and_email_are_available(
        self, async_client: AsyncClient, encoded_store: str
    ):
        body = await _check(
            async_client,
            encoded_store,
            _unique("qauser"),
            f"{_unique('qa')}@example.com",
            "direct",
        )
        assert body["username_available"] is True
        assert body["email_available"] is True
        assert body["resumable"] is False

    @pytest.mark.asyncio
    async def test_existing_user_username_is_unavailable(
        self, async_client: AsyncClient, encoded_store: str, test_users: dict
    ):
        taken = "testadmin"  # seed 계정 (test_users fixture 가 보장)
        body = await _check(
            async_client,
            encoded_store,
            taken,
            f"{_unique('qa')}@example.com",
            "direct",
        )
        assert body["username_available"] is False
        assert body["email_available"] is True

    @pytest.mark.asyncio
    async def test_verified_user_email_is_unavailable(
        self, async_client: AsyncClient, encoded_store: str, db
    ):
        from app.models.user import User

        user = (
            await db.execute(
                select(User).where(
                    User.email.is_not(None),
                    User.email_verified == True,  # noqa: E712
                )
            )
        ).scalars().first()
        assert user is not None, "seed 에 인증된 이메일을 가진 사용자가 필요하다"

        body = await _check(
            async_client, encoded_store, _unique("qauser"), user.email, "direct"
        )
        assert body["email_available"] is False

    @pytest.mark.asyncio
    async def test_candidate_username_blocks_direct_signup(
        self, async_client: AsyncClient, encoded_store: str, make_candidate
    ):
        """direct 는 candidates 의 아이디와도 충돌한다 (전역 unique)."""
        username = _unique("qacand")
        await make_candidate(username, f"{_unique('qa')}@example.com")

        body = await _check(
            async_client,
            encoded_store,
            username,
            f"{_unique('qa')}@example.com",
            "direct",
        )
        assert body["username_available"] is False


class TestJoinMode:
    """mode="join" — /applications/start 와 같은 규칙."""

    @pytest.mark.asyncio
    async def test_fresh_pair_is_available(
        self, async_client: AsyncClient, encoded_store: str
    ):
        body = await _check(
            async_client,
            encoded_store,
            _unique("qauser"),
            f"{_unique('qa')}@example.com",
            "join",
        )
        assert body["username_available"] is True
        assert body["email_available"] is True
        assert body["resumable"] is False

    @pytest.mark.asyncio
    async def test_same_username_and_email_is_resumable_not_taken(
        self, async_client: AsyncClient, encoded_store: str, make_candidate
    ):
        """둘 다 일치 = 기존 지원자가 이어서 진행하는 정상 경로. 막으면 안 된다."""
        username = _unique("qacand")
        email = f"{_unique('qa')}@example.com"
        await make_candidate(username, email)

        body = await _check(async_client, encoded_store, username, email, "join")
        assert body["resumable"] is True
        assert body["username_available"] is True
        assert body["email_available"] is True

    @pytest.mark.asyncio
    async def test_username_only_match_blocks_username(
        self, async_client: AsyncClient, encoded_store: str, make_candidate
    ):
        username = _unique("qacand")
        await make_candidate(username, f"{_unique('qa')}@example.com")

        body = await _check(
            async_client,
            encoded_store,
            username,
            f"{_unique('qa')}@example.com",
            "join",
        )
        assert body["username_available"] is False
        assert body["email_available"] is True
        assert body["resumable"] is False

    @pytest.mark.asyncio
    async def test_email_only_match_blocks_email(
        self, async_client: AsyncClient, encoded_store: str, make_candidate
    ):
        email = f"{_unique('qa')}@example.com"
        await make_candidate(_unique("qacand"), email)

        body = await _check(
            async_client, encoded_store, _unique("qauser"), email, "join"
        )
        assert body["email_available"] is False
        assert body["username_available"] is True
        assert body["resumable"] is False

    @pytest.mark.asyncio
    async def test_split_credentials_block_both(
        self, async_client: AsyncClient, encoded_store: str, make_candidate
    ):
        """아이디와 이메일이 서로 다른 지원자에 묶인 경우 (credentials_split)."""
        username = _unique("qacand")
        email = f"{_unique('qa')}@example.com"
        await make_candidate(username, f"{_unique('qa')}@example.com")
        await make_candidate(_unique("qacand"), email)

        body = await _check(async_client, encoded_store, username, email, "join")
        assert body["username_available"] is False
        assert body["email_available"] is False
        assert body["resumable"] is False

    @pytest.mark.asyncio
    async def test_org_user_username_blocks_new_candidate(
        self, async_client: AsyncClient, encoded_store: str, test_users: dict
    ):
        """지원자가 없을 때는 그 매장 org 의 users 아이디와 충돌한다."""
        taken = "teststaff"  # seed 계정 (test_users fixture 가 보장)
        body = await _check(
            async_client,
            encoded_store,
            taken,
            f"{_unique('qa')}@example.com",
            "join",
        )
        assert body["username_available"] is False


class TestLinkErrors:
    @pytest.mark.asyncio
    async def test_malformed_encoded_returns_invalid_link(
        self, async_client: AsyncClient
    ):
        res = await async_client.post(
            ENDPOINT,
            json={
                "encoded": "!!!not-base64!!!",
                "username": "someone",
                "email": "someone@example.com",
                "mode": "join",
            },
        )
        assert res.status_code == 404
        assert res.json()["detail"]["code"] == "invalid_link"

    @pytest.mark.asyncio
    async def test_unknown_store_returns_store_not_found(
        self, async_client: AsyncClient
    ):
        res = await async_client.post(
            ENDPOINT,
            json={
                "encoded": encode_uuid(uuid.uuid4()),
                "username": "someone",
                "email": "someone@example.com",
                "mode": "join",
            },
        )
        assert res.status_code == 404
        assert res.json()["detail"]["code"] == "store_not_found"
