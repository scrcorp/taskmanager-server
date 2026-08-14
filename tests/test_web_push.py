"""웹 푸시 — unit + API integration (merge gate).

검증하는 불변식:

- **공개키는 서버가 내려준다.** 빌드에 박으면 구독-발송 키 불일치가 조용히
  발생하므로, config 엔드포인트가 실제 설정값을 그대로 준다.
- **endpoint 는 기기 식별자다.** 같은 endpoint 재등록은 행을 늘리지 않고,
  주인이 바뀌면 소유자가 이전된다(공용 단말에서 계정 교체).
- **404/410 은 확정 사망** → 구독 행 삭제. 그 외 실패는 행을 남긴다.
  (죽은 구독이 남으면 영원히 헛발송하고, 살아있는 걸 지우면 알림이 끊긴다)
- **푸시 실패가 본 작업을 깨지 않는다.** send_to_user 는 예외를 던지지 않는다.
- **선호 변경은 이력으로 남는다.** "그 시점에 껐는지" 를 증명할 수 있어야 한다.
- **push 채널은 in_app 과 독립.** 푸시를 꺼도 알림함에는 쌓인다.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from pywebpush import WebPushException
from sqlalchemy import delete, select

from app.core.alert_categories import (
    is_in_app_enabled,
    is_push_enabled,
    normalize_preferences,
)
from app.database import async_session
from app.models.alert_preference_audit import AlertPreferenceAudit
from app.models.alert import Alert
from app.models.organization import Organization
from app.models.push_delivery import (
    DELIVERY_ACCEPTED,
    DELIVERY_GONE,
    DELIVERY_SKIPPED,
    SKIP_NO_SUBSCRIPTION,
    PushDelivery,
)
from app.models.push_subscription import PushSubscription
from app.models.user import User
import app.services.push_digest_service as digest
from app.services.push_service import push_service

PUSH = "/api/v1/app/my/push"
PREFS = "/api/v1/app/profile/alert-preferences"


def _sub_body(suffix: str = "a") -> dict:
    return {
        "endpoint": f"https://fcm.googleapis.com/fcm/send/test-{suffix}",
        "keys": {"p256dh": "BNcRdTESTKEY", "auth": "tBHITESTAUTH"},
        "user_agent": "pytest",
    }


@pytest_asyncio.fixture
async def staff_headers(test_users) -> dict[str, str]:
    """teststaff 앱 로그인 헤더."""
    from httpx import ASGITransport

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/app/auth/login",
            json={"username": "teststaff", "password": "1234"},
        )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest_asyncio.fixture(autouse=True)
async def _isolate_push(test_users):
    """구독·선호·이력을 테스트마다 초기화.

    send_to_user 는 그 사용자의 **모든 기기**로 팬아웃하므로, 남아 있는 구독이
    하나라도 있으면 집계 단언이 흔들린다. 선호값도 마찬가지로 None(미설정)에서
    시작해야 "미설정 → False" 전이를 검증할 수 있다.
    (개발 중 브라우저로 만든 실제 구독도 지워진다 — 필요하면 앱에서 다시 켜면 된다)
    """
    user_id = test_users["teststaff"]["id"]

    async def _reset() -> None:
        async with async_session() as db:
            await db.execute(
                delete(PushSubscription).where(PushSubscription.user_id == user_id)
            )
            await db.execute(
                delete(AlertPreferenceAudit).where(AlertPreferenceAudit.user_id == user_id)
            )
            await db.execute(
                delete(PushDelivery).where(PushDelivery.user_id == user_id)
            )
            user = await db.get(User, user_id)
            if user is not None:
                user.alert_preferences = {}
            await db.commit()

    await _reset()
    yield
    await _reset()


# ---------------------------------------------------------------------------
# unit — 채널 선호 계산
# ---------------------------------------------------------------------------


def test_push_defaults_to_on_when_unset() -> None:
    """미설정이면 켜진 것으로 본다 — 신규 사용자가 알림을 놓치면 안 된다."""
    assert is_push_enabled(None, "schedule") is True
    assert is_push_enabled({}, "schedule") is True
    assert is_push_enabled({"schedule": {}}, "schedule") is True


def test_push_and_in_app_are_independent() -> None:
    """푸시를 꺼도 in_app 은 살아 있다 — 폰은 조용하지만 알림함에는 쌓인다."""
    prefs = {"schedule": {"push": False}}
    assert is_push_enabled(prefs, "schedule") is False
    assert is_in_app_enabled(prefs, "schedule") is True


def test_normalize_keeps_push_and_drops_unknown() -> None:
    cleaned = normalize_preferences(
        {"schedule": {"push": False, "bogus": 1}, "nope": {"push": True}}
    )
    assert cleaned == {"schedule": {"push": False}}


# ---------------------------------------------------------------------------
# unit — 발송 결과 처리
# ---------------------------------------------------------------------------


def _webpush_error(status: int) -> WebPushException:
    class _Resp:
        status_code = status

    exc = WebPushException("boom")
    exc.response = _Resp()  # type: ignore[attr-defined]
    return exc


@pytest.mark.asyncio
async def test_dead_subscription_is_removed_on_410(test_users) -> None:
    """410 = 구독 사망. 남겨두면 영원히 헛발송한다."""
    user = test_users["teststaff"]
    async with async_session() as db:
        db.add(
            PushSubscription(
                organization_id=user["organization_id"],
                user_id=user["id"],
                endpoint="https://fcm.googleapis.com/fcm/send/test-dead",
                p256dh="k",
                auth="a",
            )
        )
        await db.commit()

    with patch("app.services.push_service._send_one_blocking", side_effect=_webpush_error(410)):
        async with async_session() as db:
            result = await push_service.send_to_user(
                db, user["id"], title="t", body="b"
            )
            await db.commit()

    assert result.removed == 1 and result.sent == 0
    async with async_session() as db:
        rows = (
            await db.execute(
                select(PushSubscription).where(PushSubscription.user_id == user["id"])
            )
        ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_transient_failure_keeps_subscription(test_users) -> None:
    """500 은 일시적일 수 있다 — 지우면 살아있는 기기의 알림이 끊긴다."""
    user = test_users["teststaff"]
    async with async_session() as db:
        db.add(
            PushSubscription(
                organization_id=user["organization_id"],
                user_id=user["id"],
                endpoint="https://fcm.googleapis.com/fcm/send/test-flaky",
                p256dh="k",
                auth="a",
            )
        )
        await db.commit()

    with patch("app.services.push_service._send_one_blocking", side_effect=_webpush_error(500)):
        async with async_session() as db:
            result = await push_service.send_to_user(db, user["id"], title="t", body="b")
            await db.commit()

    assert result.failed == 1 and result.removed == 0
    async with async_session() as db:
        row = (
            await db.execute(
                select(PushSubscription).where(
                    PushSubscription.endpoint
                    == "https://fcm.googleapis.com/fcm/send/test-flaky"
                )
            )
        ).scalar_one()
        assert row.failure_count == 1


@pytest.mark.asyncio
async def test_send_never_raises(test_users) -> None:
    """푸시 실패가 스케줄 승인 같은 본 작업을 롤백시키면 안 된다."""
    user = test_users["teststaff"]
    async with async_session() as db:
        db.add(
            PushSubscription(
                organization_id=user["organization_id"],
                user_id=user["id"],
                endpoint="https://fcm.googleapis.com/fcm/send/test-boom",
                p256dh="k",
                auth="a",
            )
        )
        await db.commit()

    with patch("app.services.push_service._send_one_blocking", side_effect=RuntimeError("x")):
        async with async_session() as db:
            result = await push_service.send_to_user(db, user["id"], title="t", body="b")
            await db.commit()
    assert result.failed == 1


@pytest.mark.asyncio
async def test_no_subscriptions_is_a_noop(test_users) -> None:
    async with async_session() as db:
        result = await push_service.send_to_user(
            db, test_users["teststaff"]["id"], title="t", body="b"
        )
    assert result.attempted == 0 and result.errors == []


# ---------------------------------------------------------------------------
# API — 구독 등록/해지
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_exposes_public_key(
    async_client: AsyncClient, staff_headers: dict
) -> None:
    """공개키는 서버가 내려준다 — 빌드에 박으면 키 불일치가 조용히 생긴다."""
    resp = await async_client.get(f"{PUSH}/config", headers=staff_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"enabled", "vapid_public_key"}
    if body["enabled"]:
        assert body["vapid_public_key"]


@pytest.mark.asyncio
async def test_subscribe_is_idempotent_per_endpoint(
    async_client: AsyncClient, staff_headers: dict
) -> None:
    """같은 endpoint 재등록은 행을 늘리지 않는다 — 늘면 알림이 두 번 간다."""
    first = await async_client.post(
        f"{PUSH}/subscribe", json=_sub_body("dup"), headers=staff_headers
    )
    if first.status_code == 503:
        pytest.skip("이 환경에는 VAPID 키가 없다")
    assert first.status_code == 200, first.text
    second = await async_client.post(
        f"{PUSH}/subscribe", json=_sub_body("dup"), headers=staff_headers
    )
    assert second.status_code == 200
    assert second.json()["device_count"] == first.json()["device_count"]


@pytest.mark.asyncio
async def test_unsubscribe_is_forgiving(
    async_client: AsyncClient, staff_headers: dict
) -> None:
    """없는 endpoint 해지도 성공 — 이미 정리된 상태가 원하는 결과다."""
    resp = await async_client.post(
        f"{PUSH}/unsubscribe",
        json={"endpoint": "https://fcm.googleapis.com/fcm/send/test-ghost"},
        headers=staff_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["subscribed"] is False


@pytest.mark.asyncio
async def test_subscribe_requires_auth(async_client: AsyncClient) -> None:
    resp = await async_client.post(f"{PUSH}/subscribe", json=_sub_body())
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# API — 선호 변경 + 감사 이력
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_preference_change_is_audited(
    async_client: AsyncClient, staff_headers: dict, test_users
) -> None:
    """"언제 껐는지" 가 남아야 나중에 근거가 된다."""
    off = await async_client.put(
        PREFS, json={"preferences": {"schedule": {"push": False}}}, headers=staff_headers
    )
    assert off.status_code == 200, off.text
    assert off.json()["preferences"]["schedule"]["push"] is False

    on = await async_client.put(
        PREFS, json={"preferences": {"schedule": {"push": True}}}, headers=staff_headers
    )
    assert on.status_code == 200
    assert on.json()["preferences"]["schedule"]["push"] is True

    async with async_session() as db:
        rows = (
            await db.execute(
                select(AlertPreferenceAudit)
                .where(AlertPreferenceAudit.category_code == "schedule")
                .where(AlertPreferenceAudit.channel == "push")
                .order_by(AlertPreferenceAudit.changed_at)
            )
        ).scalars().all()

    assert len(rows) == 2
    # None(미설정) → False → True. None 과 False 를 구분해야 "손댄 적 없음" 과
    # "명시적으로 껐음" 이 섞이지 않는다.
    assert (rows[0].old_value, rows[0].new_value) == (None, False)
    assert (rows[1].old_value, rows[1].new_value) == (False, True)
    # 본인 변경이므로 주체와 대상이 같다.
    assert rows[0].user_id == rows[0].changed_by_user_id


@pytest.mark.asyncio
async def test_unchanged_preference_writes_no_audit_row(
    async_client: AsyncClient, staff_headers: dict
) -> None:
    """같은 값 재저장은 이력을 남기지 않는다 — 노이즈가 쌓이면 증거가 흐려진다."""
    await async_client.put(
        PREFS, json={"preferences": {"notice": {"push": False}}}, headers=staff_headers
    )
    await async_client.put(
        PREFS, json={"preferences": {"notice": {"push": False}}}, headers=staff_headers
    )
    async with async_session() as db:
        rows = (
            await db.execute(
                select(AlertPreferenceAudit).where(
                    AlertPreferenceAudit.category_code == "notice"
                )
            )
        ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_categories_expose_push_availability(
    async_client: AsyncClient, staff_headers: dict
) -> None:
    """클라가 메타만 보고 격자를 그릴 수 있어야 한다."""
    resp = await async_client.get(PREFS, headers=staff_headers)
    assert resp.status_code == 200
    cats = resp.json()["categories"]
    assert cats and all(c["push_available"] is True for c in cats)


# ---------------------------------------------------------------------------
# 발송 기록 (push_deliveries) — "우리는 보냈다" 의 근거
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accepted_delivery_is_recorded(test_users) -> None:
    """수락된 발송이 기록으로 남아야 나중에 조회할 수 있다."""
    user = test_users["teststaff"]
    async with async_session() as db:
        db.add(
            PushSubscription(
                organization_id=user["organization_id"],
                user_id=user["id"],
                endpoint="https://fcm.googleapis.com/fcm/send/test-rec",
                p256dh="k",
                auth="a",
            )
        )
        await db.commit()

    with patch("app.services.push_service._send_one_blocking", return_value=None):
        async with async_session() as db:
            await push_service.send_to_user(
                db, user["id"], title="T", body="B", alert_type="notice"
            )
            await db.commit()

    async with async_session() as db:
        row = (
            await db.execute(
                select(PushDelivery).where(PushDelivery.user_id == user["id"])
            )
        ).scalar_one()
    # 'accepted' 이지 'delivered' 가 아니다 — 중계 서버가 받았을 뿐이다.
    assert row.status == DELIVERY_ACCEPTED
    assert row.alert_type == "notice"
    assert row.subscription_endpoint.endswith("test-rec")


@pytest.mark.asyncio
async def test_gone_delivery_records_before_cleanup(test_users) -> None:
    """구독 행은 지워져도 어느 기기였는지는 기록에 남는다."""
    user = test_users["teststaff"]
    async with async_session() as db:
        db.add(
            PushSubscription(
                organization_id=user["organization_id"],
                user_id=user["id"],
                endpoint="https://fcm.googleapis.com/fcm/send/test-gone",
                p256dh="k",
                auth="a",
            )
        )
        await db.commit()

    with patch("app.services.push_service._send_one_blocking", side_effect=_webpush_error(404)):
        async with async_session() as db:
            await push_service.send_to_user(db, user["id"], title="T", body="B")
            await db.commit()

    async with async_session() as db:
        row = (
            await db.execute(
                select(PushDelivery).where(PushDelivery.user_id == user["id"])
            )
        ).scalar_one()
    assert row.status == DELIVERY_GONE and row.http_status == 404
    assert row.subscription_endpoint.endswith("test-gone")


@pytest.mark.asyncio
async def test_no_subscription_is_recorded_as_skipped(test_users) -> None:
    """조용한 무발송이 가장 설명하기 어렵다 — 안 보낸 사실도 남긴다."""
    user = test_users["teststaff"]
    async with async_session() as db:
        await push_service.send_to_user(
            db,
            user["id"],
            title="T",
            body="B",
            organization_id=user["organization_id"],
        )
        await db.commit()

    async with async_session() as db:
        row = (
            await db.execute(
                select(PushDelivery).where(PushDelivery.user_id == user["id"])
            )
        ).scalar_one()
    assert row.status == DELIVERY_SKIPPED
    assert row.skip_reason == SKIP_NO_SUBSCRIPTION


# ---------------------------------------------------------------------------
# 다이제스트
# ---------------------------------------------------------------------------


def test_digest_body_is_singular_and_plural() -> None:
    assert digest._digest_body(1) == "You have 1 unread notification."
    assert digest._digest_body(3) == "You have 3 unread notifications."


@pytest.mark.asyncio
async def test_digest_skips_when_nothing_unread(test_users, seed_organization) -> None:
    """읽을 게 없으면 보내지 않는다 — 빈 다이제스트는 순수 소음이다."""
    user = test_users["teststaff"]
    async with async_session() as db:
        db.add(
            PushSubscription(
                organization_id=user["organization_id"],
                user_id=user["id"],
                endpoint="https://fcm.googleapis.com/fcm/send/test-digest-none",
                p256dh="k",
                auth="a",
            )
        )
        await db.execute(delete(Alert).where(Alert.user_id == user["id"]))
        await db.commit()

    async with async_session() as db:
        org = await db.get(Organization, seed_organization["id"])
        sent = await digest.send_digests_for_organization(db, org)
        await db.commit()
    assert sent == 0


@pytest.mark.asyncio
async def test_digest_sends_once_per_day(test_users, seed_organization) -> None:
    """같은 날 두 번 보내지 않는다 — 기록 자체가 중복 판정의 근거다."""
    user = test_users["teststaff"]
    async with async_session() as db:
        db.add(
            PushSubscription(
                organization_id=user["organization_id"],
                user_id=user["id"],
                endpoint="https://fcm.googleapis.com/fcm/send/test-digest",
                p256dh="k",
                auth="a",
            )
        )
        db.add(
            Alert(
                organization_id=user["organization_id"],
                user_id=user["id"],
                type="notice",
                message="unread one",
            )
        )
        await db.commit()

    with patch("app.services.push_service._send_one_blocking", return_value=None):
        async with async_session() as db:
            org = await db.get(Organization, seed_organization["id"])
            first = await digest.send_digests_for_organization(db, org)
            await db.commit()
        async with async_session() as db:
            org = await db.get(Organization, seed_organization["id"])
            second = await digest.send_digests_for_organization(db, org)
            await db.commit()

    assert first == 1 and second == 0

    async with async_session() as db:
        await db.execute(delete(Alert).where(Alert.user_id == user["id"]))
        await db.commit()
