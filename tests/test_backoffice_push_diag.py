"""Backoffice Push Diagnostics (읽기 전용 조회) 테스트.

검증 축:
    1. 인증 — 로그인 없이는 못 본다 (다른 backoffice 도구와 동일 규칙)
    2. 검색 — username/name 부분일치로 사용자를 찾는다
    3. 진단 결론 — 기기가 없으면 그 사실을 단정적으로 말한다
    4. 발송 이력 — status 와 skip 사유가 그대로 드러난다
    5. 설정 변경 이력 — unset(None) 과 off(False) 를 구분해 보여준다
    6. 부작용 없음 — 이 화면은 아무것도 보내지 않는다
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.api.backoffice.deps import COOKIE_NAME
from app.config import settings
from app.database import async_session
from app.models.alert_preference_audit import AlertPreferenceAudit
from app.models.push_delivery import PushDelivery
from app.models.push_subscription import PushSubscription
from app.models.user import User

pytestmark = pytest.mark.asyncio

BASE = "/" + settings.BACKOFFICE_PATH.strip("/")
TOOL = f"{BASE}/tools/push"
USER = settings.BACKOFFICE_ADMIN_USERNAME
PW = "control1234"  # worktree .env 해시의 평문


async def _login(client: AsyncClient) -> None:
    await client.post(f"{BASE}/login", data={"username": USER, "password": PW})


@pytest_asyncio.fixture
async def staff_user(test_users) -> User:
    async with async_session() as db:
        row = (
            await db.execute(select(User).where(User.username == "teststaff"))
        ).scalar_one()
        return row


@pytest_asyncio.fixture(autouse=True)
async def _clean(test_users):
    """이 파일이 만든 행만 정리 — 다른 테스트의 잔여 상태에 영향받지 않게."""

    async def _reset() -> None:
        async with async_session() as db:
            user = (
                await db.execute(select(User).where(User.username == "teststaff"))
            ).scalar_one()
            await db.execute(
                delete(PushDelivery).where(PushDelivery.user_id == user.id)
            )
            await db.execute(
                delete(PushSubscription).where(PushSubscription.user_id == user.id)
            )
            await db.execute(
                delete(AlertPreferenceAudit).where(
                    AlertPreferenceAudit.user_id == user.id
                )
            )
            await db.commit()

    await _reset()
    yield
    await _reset()


# --------------------------------------------------------------------------- #
# 1. 인증
# --------------------------------------------------------------------------- #
async def test_search_requires_auth(async_client: AsyncClient) -> None:
    async_client.cookies.clear()
    resp = await async_client.get(TOOL)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


async def test_detail_requires_auth(async_client: AsyncClient, staff_user) -> None:
    async_client.cookies.clear()
    resp = await async_client.get(f"{TOOL}/{staff_user.id}")
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


# --------------------------------------------------------------------------- #
# 2. 검색
# --------------------------------------------------------------------------- #
async def test_search_finds_user_by_username(
    async_client: AsyncClient, staff_user
) -> None:
    await _login(async_client)
    resp = await async_client.get(TOOL, params={"q": "teststaff"})
    assert resp.status_code == 200
    assert "teststaff" in resp.text
    # 상세로 가는 링크가 실제 user id 를 가리켜야 한다
    assert str(staff_user.id) in resp.text


async def test_search_without_query_shows_no_table(async_client: AsyncClient) -> None:
    await _login(async_client)
    resp = await async_client.get(TOOL)
    assert resp.status_code == 200
    # 검색 전에는 결과 표 자체를 그리지 않는다
    assert "<tbody>" not in resp.text


async def test_search_miss_says_so(async_client: AsyncClient, test_users) -> None:
    await _login(async_client)
    resp = await async_client.get(TOOL, params={"q": "zzz-no-such-person-zzz"})
    assert resp.status_code == 200
    assert "No user matched" in resp.text


# --------------------------------------------------------------------------- #
# 3. 진단 결론
# --------------------------------------------------------------------------- #
async def test_detail_reports_no_device_when_unsubscribed(
    async_client: AsyncClient, staff_user
) -> None:
    """기기가 없으면 '없다' 고 단정해야 한다 — 이게 가장 흔한 원인이다."""
    await _login(async_client)
    resp = await async_client.get(f"{TOOL}/{staff_user.id}")
    assert resp.status_code == 200
    assert "No device is registered" in resp.text


async def test_detail_lists_registered_device(
    async_client: AsyncClient, staff_user
) -> None:
    async with async_session() as db:
        db.add(
            PushSubscription(
                organization_id=staff_user.organization_id,
                user_id=staff_user.id,
                endpoint="https://fcm.example.test/diag-endpoint-value",
                p256dh="p" * 20,
                auth="a" * 12,
                user_agent="DiagTestAgent/1.0",
            )
        )
        await db.commit()

    await _login(async_client)
    resp = await async_client.get(f"{TOOL}/{staff_user.id}")
    assert resp.status_code == 200
    assert "1 device(s) registered" in resp.text
    assert "DiagTestAgent/1.0" in resp.text


async def test_detail_unknown_user_is_not_found(async_client: AsyncClient) -> None:
    await _login(async_client)
    resp = await async_client.get(f"{TOOL}/{uuid.uuid4()}")
    assert resp.status_code == 200
    assert "User not found" in resp.text


# --------------------------------------------------------------------------- #
# 4. 발송 이력 — 스킵 사유가 드러나야 한다
# --------------------------------------------------------------------------- #
async def test_detail_shows_skip_reason(
    async_client: AsyncClient, staff_user
) -> None:
    """'보냈는데 안 왔다' 의 답이 preference_off 인 경우를 화면이 말해줘야 한다."""
    async with async_session() as db:
        db.add(
            PushDelivery(
                organization_id=staff_user.organization_id,
                user_id=staff_user.id,
                alert_type="notice",
                status="skipped",
                skip_reason="preference_off",
                title="Store notice",
            )
        )
        await db.commit()

    await _login(async_client)
    resp = await async_client.get(f"{TOOL}/{staff_user.id}")
    assert resp.status_code == 200
    assert "skipped" in resp.text
    assert "preference_off" in resp.text
    assert "Store notice" in resp.text


async def test_detail_shows_accepted_delivery(
    async_client: AsyncClient, staff_user
) -> None:
    async with async_session() as db:
        db.add(
            PushDelivery(
                organization_id=staff_user.organization_id,
                user_id=staff_user.id,
                alert_type="schedule",
                status="accepted",
                title="Shift assigned",
            )
        )
        await db.commit()

    await _login(async_client)
    resp = await async_client.get(f"{TOOL}/{staff_user.id}")
    assert resp.status_code == 200
    assert "accepted" in resp.text
    assert "Shift assigned" in resp.text


async def test_detail_says_when_no_delivery_recorded(
    async_client: AsyncClient, staff_user
) -> None:
    await _login(async_client)
    resp = await async_client.get(f"{TOOL}/{staff_user.id}")
    assert "No delivery attempt recorded" in resp.text


# --------------------------------------------------------------------------- #
# 5. 설정 변경 이력 — unset 과 off 를 구분
# --------------------------------------------------------------------------- #
async def test_detail_shows_preference_audit(
    async_client: AsyncClient, staff_user
) -> None:
    """'너가 껐다' 를 증명하는 화면. unset→off 전이가 그대로 보여야 한다."""
    async with async_session() as db:
        db.add(
            AlertPreferenceAudit(
                organization_id=staff_user.organization_id,
                user_id=staff_user.id,
                changed_by_user_id=staff_user.id,
                category_code="notice",
                channel="push",
                old_value=None,
                new_value=False,
            )
        )
        await db.commit()

    await _login(async_client)
    resp = await async_client.get(f"{TOOL}/{staff_user.id}")
    assert resp.status_code == 200
    assert "notice" in resp.text
    assert "unset" in resp.text  # None 은 off 가 아니라 unset 으로 표시
    assert "self" in resp.text  # 본인이 바꾼 것


async def test_detail_says_when_no_audit(
    async_client: AsyncClient, staff_user
) -> None:
    await _login(async_client)
    resp = await async_client.get(f"{TOOL}/{staff_user.id}")
    assert "never changed a notification setting" in resp.text


# --------------------------------------------------------------------------- #
# 6. 부작용 없음
# --------------------------------------------------------------------------- #
async def test_viewing_sends_nothing_and_writes_nothing(
    async_client: AsyncClient, staff_user
) -> None:
    """조회 화면은 읽기 전용이다 — 보는 것만으로 발송 기록이 생기면 안 된다."""
    await _login(async_client)
    await async_client.get(f"{TOOL}/{staff_user.id}")

    async with async_session() as db:
        rows = (
            await db.execute(
                select(PushDelivery).where(PushDelivery.user_id == staff_user.id)
            )
        ).scalars().all()
    assert rows == []


async def test_tool_exposes_no_post_route() -> None:
    """발송 버튼을 실수로 붙이지 않았는지 — P1 은 조회 전용이다."""
    from app.api.backoffice.tools.push_diag import router

    methods: set[str] = set()
    for route in router.routes:
        methods |= set(getattr(route, "methods", set()))
    assert methods <= {"GET", "HEAD"}, f"read-only 여야 하는데 {methods} 가 있다"
