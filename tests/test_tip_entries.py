"""Stage A 팁 입력·분배 API 통합 테스트.

대상 엔드포인트:
    POST   /api/v1/app/my/tips/entries
    PATCH  /api/v1/app/my/tips/entries/{entry_id}
    GET    /api/v1/app/my/tips/entries
    GET    /api/v1/app/my/tips/distributions/incoming
    POST   /api/v1/app/my/tips/distributions/{id}/accept

테스트는 conftest 의 testadmin/testgm/testsv/teststaff (pw=1234) 계정 + 공용 test_store_id 를 사용한다.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import AsyncIterator
from uuid import UUID  # noqa: F401

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.main import app
from app.models.tip import TipAuditLog, TipDistribution, TipEntry
from app.models.user_store import UserStore


# ── 권한 sync — startup 이벤트가 ASGITransport 에서 자동 실행 안 됨 ──

@pytest_asyncio.fixture(scope="session", autouse=True)
async def _ensure_tip_permissions_synced() -> None:
    """tips:* permission 과 role_permissions 매핑을 DB 에 보장.

    프로덕션에서는 서버 startup 이벤트가 자동 sync 하지만 테스트에서는
    수동 호출. 이미 있으면 no-op.
    """
    from app.main import sync_permission_registry, sync_default_role_permissions
    await sync_permission_registry()
    await sync_default_role_permissions()


# ── teststaff/testsv 를 test_store_id 에 매핑 (user_stores) ──

@pytest_asyncio.fixture(scope="session", autouse=True)
async def _ensure_user_store_mapping(test_users: dict, test_store_id: UUID) -> None:
    """tip API 는 check_store_access 를 거치는데, teststaff/testsv 가
    test_store_id 에 user_stores 매핑이 없으면 403. 이미 있으면 no-op.
    """
    async with async_session() as db:
        for name in ("teststaff", "testsv"):
            uid = test_users[name]["id"]
            existing = await db.scalar(select(UserStore).where(
                UserStore.user_id == uid, UserStore.store_id == test_store_id,
            ))
            if existing is None:
                db.add(UserStore(user_id=uid, store_id=test_store_id))
        await db.commit()


# ── 토큰 fixture (app 로그인) ─────────────────────────────────

async def _app_login(username: str) -> str:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/app/auth/login",
            json={"username": username, "password": "1234"},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def staff_headers() -> dict[str, str]:
    token = await _app_login("teststaff")
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def sv_headers() -> dict[str, str]:
    token = await _app_login("testsv")
    return {"Authorization": f"Bearer {token}"}


# ── tip 데이터 정리 ────────────────────────────────────────────
#
# 주의: 같은 worktree DB 를 사용자 수동 검증과 자동 테스트가 공유한다.
# fixture 가 단순히 "test 계정의 모든 row 삭제" 하면 사용자가 매뉴얼로 만든
# entry 까지 날아간다. 그래서 fixture 시작 시각을 기록하고 그 이후 생성된
# row 만 정리한다.


async def _purge_tip_data_since(
    employee_ids: list[UUID], since: datetime,
) -> None:
    async with async_session() as db:
        await db.execute(
            delete(TipEntry).where(
                TipEntry.employee_id.in_(employee_ids),
                TipEntry.created_at >= since,
            )
        )
        await db.execute(
            delete(TipAuditLog).where(
                TipAuditLog.actor_id.in_(employee_ids),
                TipAuditLog.created_at >= since,
            )
        )
        await db.commit()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tip_state(test_users) -> AsyncIterator[None]:
    ids = [u["id"] for u in test_users.values()]
    start = datetime.now(timezone.utc)
    yield
    await _purge_tip_data_since(ids, start)


# ── schedule 픽스처 — 한 사용자에 대해 today schedule 만들고 id 반환 ──────

@pytest_asyncio.fixture
async def staff_schedule(make_schedule, test_users) -> str:
    sid = await make_schedule(test_users["teststaff"])
    return str(sid)


@pytest_asyncio.fixture
async def sv_schedule(make_schedule, test_users) -> str:
    sid = await make_schedule(test_users["testsv"])
    return str(sid)


# ── 헬퍼 ──────────────────────────────────────────────────────

def _entry_payload(schedule_id: str, *, card: str = "100.00", cash: str = "20.00",
                   distributions: list | None = None) -> dict:
    return {
        "schedule_id": schedule_id,
        "card_tips": card,
        "cash_tips_kept": cash,
        "source": "staff_app",
        "distributions": distributions or [],
    }


# ── Tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_entry_no_distribution(
    async_client: AsyncClient,
    staff_headers: dict,
    staff_schedule: str,
):
    """분배 없는 단순 entry 생성 — 계산값이 입력 그대로 반영."""
    payload = _entry_payload(staff_schedule, card="80.00", cash="20.00")
    resp = await async_client.post(
        "/api/v1/app/my/tips/entries", json=payload, headers=staff_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["schedule_id"] == staff_schedule
    assert Decimal(body["card_tips"]) == Decimal("80.00")
    assert Decimal(body["cash_tips_kept"]) == Decimal("20.00")
    assert Decimal(body["distributed_total"]) == Decimal("0")
    assert Decimal(body["reportable_card"]) == Decimal("80.00")
    assert Decimal(body["reported_on_4070"]) == Decimal("100.00")
    assert body["distributions"] == []
    assert body["source"] == "staff_app"


@pytest.mark.asyncio
async def test_create_entry_with_distribution(
    async_client: AsyncClient,
    staff_headers: dict,
    staff_schedule: str,
    test_users: dict,
):
    """분배 포함 entry — 분배합만큼 reportable_card 가 줄어든다."""
    receiver_id = test_users["testsv"]["id"]
    payload = _entry_payload(
        staff_schedule, card="100.00", cash="20.00",
        distributions=[{"receiver_id": str(receiver_id), "amount": "15.00", "reason": "Bar share"}],
    )
    resp = await async_client.post(
        "/api/v1/app/my/tips/entries", json=payload, headers=staff_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert Decimal(body["distributed_total"]) == Decimal("15.00")
    assert Decimal(body["reportable_card"]) == Decimal("85.00")
    assert Decimal(body["reported_on_4070"]) == Decimal("105.00")
    assert len(body["distributions"]) == 1
    d = body["distributions"][0]
    assert d["status"] == "pending"
    assert d["receiver_id"] == str(receiver_id)
    assert d["receiver_name"]  # snapshot 이 채워졌어야 함


@pytest.mark.asyncio
async def test_create_entry_distribution_exceeds_card_blocks(
    async_client: AsyncClient,
    staff_headers: dict,
    staff_schedule: str,
    test_users: dict,
):
    """분배 합 > card_tips 면 422(Pydantic) 또는 400(서비스). 양쪽 다 차단되어야 한다."""
    receiver_id = test_users["testsv"]["id"]
    payload = _entry_payload(
        staff_schedule, card="10.00", cash="5.00",
        distributions=[{"receiver_id": str(receiver_id), "amount": "50.00"}],
    )
    resp = await async_client.post(
        "/api/v1/app/my/tips/entries", json=payload, headers=staff_headers,
    )
    assert resp.status_code in (400, 422), resp.text


@pytest.mark.asyncio
async def test_list_my_entries_filters_by_period(
    async_client: AsyncClient,
    staff_headers: dict,
    staff_schedule: str,
):
    """기간 필터 — 범위 밖은 제외, 범위 안만 반환."""
    today = date.today()
    # 오늘 1건 생성 — 같은 worktree DB 에 사용자가 만든 row 가 있을 수 있으므로
    # 응답에서 우리 entry id 만 검증.
    resp = await async_client.post(
        "/api/v1/app/my/tips/entries",
        json=_entry_payload(staff_schedule, card="50", cash="10"),
        headers=staff_headers,
    )
    assert resp.status_code == 201
    my_entry_id = resp.json()["id"]

    # 오늘 포함 범위 — 우리 entry 가 있어야 함
    resp_in = await async_client.get(
        "/api/v1/app/my/tips/entries",
        params={"start": today.isoformat(), "end": today.isoformat()},
        headers=staff_headers,
    )
    assert resp_in.status_code == 200
    items_in = resp_in.json()
    assert any(e["id"] == my_entry_id for e in items_in)

    # 어제~어제 — 비어있어야 함
    yesterday = today.replace(day=max(1, today.day - 1)) if today.day > 1 else today
    if yesterday != today:
        resp_out = await async_client.get(
            "/api/v1/app/my/tips/entries",
            params={"start": yesterday.isoformat(), "end": yesterday.isoformat()},
            headers=staff_headers,
        )
        assert resp_out.status_code == 200
        assert resp_out.json() == []


@pytest.mark.asyncio
async def test_cannot_update_other_user_entry(
    async_client: AsyncClient,
    staff_headers: dict,
    sv_headers: dict,
    sv_schedule: str,
):
    """다른 사람 entry 수정 시도 → 403."""
    # SV 가 자기 entry 생성
    resp = await async_client.post(
        "/api/v1/app/my/tips/entries",
        json=_entry_payload(sv_schedule, card="60", cash="10"),
        headers=sv_headers,
    )
    assert resp.status_code == 201
    sv_entry_id = resp.json()["id"]

    # staff 가 SV 의 entry 수정 시도
    resp2 = await async_client.patch(
        f"/api/v1/app/my/tips/entries/{sv_entry_id}",
        json={"card_tips": "999"},
        headers=staff_headers,
    )
    assert resp2.status_code == 403, resp2.text


@pytest.mark.asyncio
async def test_incoming_and_accept_distribution(
    async_client: AsyncClient,
    staff_headers: dict,
    sv_headers: dict,
    staff_schedule: str,
    test_users: dict,
):
    """staff 가 SV 에게 분배 → SV 의 incoming 에 보이고, accept 후 status=accepted."""
    sv_id = test_users["testsv"]["id"]
    resp = await async_client.post(
        "/api/v1/app/my/tips/entries",
        json=_entry_payload(
            staff_schedule, card="100", cash="20",
            distributions=[{"receiver_id": str(sv_id), "amount": "25", "reason": "Help"}],
        ),
        headers=staff_headers,
    )
    assert resp.status_code == 201
    dist_id = resp.json()["distributions"][0]["id"]

    # SV 의 incoming 조회
    inc = await async_client.get(
        "/api/v1/app/my/tips/distributions/incoming", headers=sv_headers,
    )
    assert inc.status_code == 200
    items = inc.json()
    matched = [d for d in items if d["id"] == dist_id]
    assert matched, items
    assert matched[0]["status"] == "pending"
    assert Decimal(matched[0]["amount"]) == Decimal("25")

    # accept
    acc = await async_client.post(
        f"/api/v1/app/my/tips/distributions/{dist_id}/accept", headers=sv_headers,
    )
    assert acc.status_code == 200, acc.text
    body = acc.json()
    assert body["status"] == "accepted"
    assert body["accepted_at"] is not None


@pytest.mark.asyncio
async def test_cannot_accept_distribution_for_other_user(
    async_client: AsyncClient,
    staff_headers: dict,
    sv_headers: dict,
    staff_schedule: str,
    test_users: dict,
):
    """다른 사람에게 보낸 분배는 본인이 accept 할 수 없다 → 403."""
    sv_id = test_users["testsv"]["id"]
    resp = await async_client.post(
        "/api/v1/app/my/tips/entries",
        json=_entry_payload(
            staff_schedule, card="100", cash="20",
            distributions=[{"receiver_id": str(sv_id), "amount": "10"}],
        ),
        headers=staff_headers,
    )
    assert resp.status_code == 201
    dist_id = resp.json()["distributions"][0]["id"]

    # staff (보낸 사람) 가 본인 분배를 accept 시도 → 403 (receiver 아님)
    bad = await async_client.post(
        f"/api/v1/app/my/tips/distributions/{dist_id}/accept", headers=staff_headers,
    )
    assert bad.status_code == 403, bad.text


@pytest.mark.asyncio
async def test_audit_log_created_on_entry_create(
    async_client: AsyncClient,
    staff_headers: dict,
    staff_schedule: str,
    test_users: dict,
    db,
):
    """entry create 시 audit log 가 entity_type=tip_entry, action=create 로 1건 이상 생성."""
    resp = await async_client.post(
        "/api/v1/app/my/tips/entries",
        json=_entry_payload(staff_schedule, card="40", cash="10"),
        headers=staff_headers,
    )
    assert resp.status_code == 201
    entry_id = UUID(resp.json()["id"])
    rows = (await db.scalars(
        select(TipAuditLog).where(
            TipAuditLog.entity_type == "tip_entry",
            TipAuditLog.entity_id == entry_id,
            TipAuditLog.action == "create",
        )
    )).all()
    assert len(rows) == 1
    log = rows[0]
    assert log.actor_id == test_users["teststaff"]["id"]
    assert log.after is not None
    assert log.after["card_tips"] == "40"


@pytest.mark.asyncio
async def test_duplicate_entry_on_same_schedule_blocks(
    async_client: AsyncClient,
    staff_headers: dict,
    staff_schedule: str,
):
    """같은 schedule 에 두 번째 entry 생성 시도 → 400."""
    payload = _entry_payload(staff_schedule, card="10", cash="0")
    r1 = await async_client.post(
        "/api/v1/app/my/tips/entries", json=payload, headers=staff_headers,
    )
    assert r1.status_code == 201
    r2 = await async_client.post(
        "/api/v1/app/my/tips/entries", json=payload, headers=staff_headers,
    )
    assert r2.status_code == 400, r2.text
    assert "already" in r2.text.lower()


@pytest.mark.asyncio
async def test_cannot_use_other_user_schedule(
    async_client: AsyncClient,
    staff_headers: dict,
    sv_schedule: str,
):
    """다른 직원의 schedule_id 로 entry 생성 시도 → 400 (schedule 매칭 실패)."""
    resp = await async_client.post(
        "/api/v1/app/my/tips/entries",
        json=_entry_payload(sv_schedule, card="10", cash="0"),
        headers=staff_headers,
    )
    assert resp.status_code == 400, resp.text
