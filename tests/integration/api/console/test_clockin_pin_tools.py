"""API integration — 콘솔 Staff PIN 도구 (lookup / directory / suggest / clear).

대상:
    - GET    /api/v1/console/users/clockin-pin/lookup     (배정 가능 여부 + 막고 있는 사람)
    - GET    /api/v1/console/users/clockin-pin/directory  (이름·PIN 으로 직원 찾기)
    - GET    /api/v1/console/users/clockin-pin/suggest    (안 쓰이는 PIN 추천)
    - DELETE /api/v1/console/users/{user_id}/clockin-pin  (PIN 제거)

핵심 계약: lookup 의 available 은 저장 경로(assert_no_pin_conflict)와 같은
판정이어야 한다. 여기가 갈리면 "available 이라 해놓고 저장은 409" 가 난다.

[작성됨]
- lookup: 안 쓰이는 PIN → available=true / holders 빈 배열
- lookup: exact 충돌 → available=false, reason=exact, holders 에 이름+PIN
- lookup: 앞자리만 겹치는 다른 길이 PIN → available=true (2026-08-13 규칙 개정)
- lookup: 4자리 미만/숫자 아님 → 422
- lookup: 리터럴 경로가 /{user_id} 로 먹히지 않는다(422 아님)
- directory: 이름 부분일치 / PIN 앞자리 / 미매칭 / 비활성 제외
- suggest: 기존 PIN 과 충돌하지 않는 길이별 PIN
- delete: PIN 제거 후 그 번호가 available 로 풀린다
- 권한 없는 계정(staff)은 403
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text

from app.database import async_session

pytestmark = pytest.mark.asyncio

LOOKUP = "/api/v1/console/users/clockin-pin/lookup"
DIRECTORY = "/api/v1/console/users/clockin-pin/directory"
SUGGEST = "/api/v1/console/users/clockin-pin/suggest"


async def _set_pin(user_id, pin: str | None) -> None:
    async with async_session() as db:
        await db.execute(
            text("UPDATE users SET clockin_pin=:pin WHERE id=:id"),
            {"pin": pin, "id": str(user_id)},
        )
        await db.commit()


@pytest_asyncio.fixture
async def pin_fixture(test_users: dict, restore_pins: None) -> dict:
    """testsv='1234', 나머지 3명은 겹치지 않는 고정 PIN 으로 세팅.

    시드 PIN 이 랜덤 6자리라 그대로 두면 '9999' 같은 후보와 우연히 겹칠 수 있다 —
    테스트가 org 전체 PIN 을 통제해야 판정이 결정적이다.
    """
    fixed = {
        "testsv": "1234",
        "testadmin": "800001",
        "testgm": "800002",
        "teststaff": "800003",
    }
    for username, pin in fixed.items():
        await _set_pin(test_users[username]["id"], pin)
    return {
        "sv_id": test_users["testsv"]["id"],
        "sv_name": test_users["testsv"]["full_name"],
        "pins": fixed,
    }


# ── lookup ────────────────────────────────────────────────────────────────


async def test_lookup_free_pin_is_available(
    async_client: AsyncClient, admin_headers: dict, pin_fixture: dict
) -> None:
    """아무도 안 쓰는 번호 → available=true, 막는 사람 없음."""
    resp = await async_client.get(
        LOOKUP, params={"pin": "5150"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is True
    assert body["reason"] is None
    assert body["holders"] == []


async def test_lookup_exact_conflict_reports_holder(
    async_client: AsyncClient, admin_headers: dict, pin_fixture: dict
) -> None:
    """정확히 같은 PIN → reason=exact + 누가 쓰는지(이름·PIN)."""
    resp = await async_client.get(
        LOOKUP, params={"pin": "1234"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "exact"
    holders = body["holders"]
    assert len(holders) == 1
    assert holders[0]["user_id"] == str(pin_fixture["sv_id"])
    assert holders[0]["full_name"] == pin_fixture["sv_name"]
    assert holders[0]["clockin_pin"] == "1234"
    assert holders[0]["conflict"] == "exact"
    assert holders[0]["role_name"]  # role join 이 살아 있는지


async def test_lookup_longer_pin_starting_with_existing_is_available(
    async_client: AsyncClient, admin_headers: dict, pin_fixture: dict
) -> None:
    """기존 `1234` 가 있어도 `123456` 은 쓸 수 있다 (구 prefix 금지 제거)."""
    resp = await async_client.get(
        LOOKUP, params={"pin": "123456"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is True
    assert body["holders"] == []


async def test_lookup_shorter_pin_that_starts_existing_is_available(
    async_client: AsyncClient, admin_headers: dict, pin_fixture: dict
) -> None:
    """반대 방향 — 기존 `800001` 이 있어도 `8000` 은 쓸 수 있다."""
    resp = await async_client.get(
        LOOKUP, params={"pin": "8000"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["available"] is True


async def test_lookup_partial_overlap_is_available(
    async_client: AsyncClient, admin_headers: dict, pin_fixture: dict
) -> None:
    """`1235` 는 `1234` 의 prefix 가 아니다 — 과잉 차단하지 않는지."""
    resp = await async_client.get(
        LOOKUP, params={"pin": "1235"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["available"] is True


@pytest.mark.parametrize("bad", ["123", "1234567", "abcd", ""])
async def test_lookup_rejects_malformed_pin(
    async_client: AsyncClient, admin_headers: dict, bad: str
) -> None:
    """4~6자리 숫자가 아니면 422 — 저장 규칙과 같은 형식 제약."""
    resp = await async_client.get(LOOKUP, params={"pin": bad}, headers=admin_headers)
    assert resp.status_code == 422


async def test_lookup_requires_permission(
    async_client: AsyncClient, test_users: dict
) -> None:
    """staff 계정은 PIN 도구를 못 쓴다 (clockin_pin:read 없음)."""
    login = await async_client.post(
        "/api/v1/app/auth/login", json={"username": "teststaff", "password": "1234"}
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    resp = await async_client.get(LOOKUP, params={"pin": "1234"}, headers=headers)
    assert resp.status_code == 403


# ── directory ─────────────────────────────────────────────────────────────


async def test_directory_matches_name(
    async_client: AsyncClient, admin_headers: dict, pin_fixture: dict
) -> None:
    """이름 부분일치 → 그 직원의 현재 PIN 이 같이 온다."""
    resp = await async_client.get(
        DIRECTORY, params={"q": "testsv"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["user_id"] for i in items] == [str(pin_fixture["sv_id"])]
    assert items[0]["clockin_pin"] == "1234"
    assert items[0]["conflict"] is None  # lookup 전용 필드


async def test_directory_matches_pin_prefix(
    async_client: AsyncClient, admin_headers: dict, pin_fixture: dict
) -> None:
    """숫자를 넣으면 PIN 앞자리로도 찾는다 — '8000' 은 3명."""
    resp = await async_client.get(
        DIRECTORY, params={"q": "8000"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    pins = {i["clockin_pin"] for i in resp.json()["items"]}
    assert pins == {"800001", "800002", "800003"}


async def test_directory_no_match_returns_empty(
    async_client: AsyncClient, admin_headers: dict, pin_fixture: dict
) -> None:
    """매칭이 없으면 빈 목록 + truncated=false."""
    resp = await async_client.get(
        DIRECTORY, params={"q": "zzz-nobody"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [], "truncated": False}


async def test_directory_excludes_inactive_by_default(
    async_client: AsyncClient, admin_headers: dict, pin_fixture: dict, test_users: dict
) -> None:
    """비활성 직원은 기본 제외, include_inactive=true 면 포함."""
    sv_id = test_users["testsv"]["id"]
    async with async_session() as db:
        await db.execute(
            text("UPDATE users SET is_active=false WHERE id=:id"), {"id": str(sv_id)}
        )
        await db.commit()
    try:
        hidden = await async_client.get(
            DIRECTORY, params={"q": "testsv"}, headers=admin_headers
        )
        assert hidden.json()["items"] == []
        shown = await async_client.get(
            DIRECTORY,
            params={"q": "testsv", "include_inactive": "true"},
            headers=admin_headers,
        )
        assert [i["user_id"] for i in shown.json()["items"]] == [str(sv_id)]
    finally:
        async with async_session() as db:
            await db.execute(
                text("UPDATE users SET is_active=true WHERE id=:id"),
                {"id": str(sv_id)},
            )
            await db.commit()


async def test_directory_requires_permission(async_client: AsyncClient) -> None:
    """staff 계정 403."""
    login = await async_client.post(
        "/api/v1/app/auth/login", json={"username": "teststaff", "password": "1234"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    resp = await async_client.get(DIRECTORY, headers=headers)
    assert resp.status_code == 403


# ── suggest ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("length", [4, 5, 6])
async def test_suggest_returns_conflict_free_pin(
    async_client: AsyncClient, admin_headers: dict, pin_fixture: dict, length: int
) -> None:
    """추천 PIN 은 요청한 자릿수 + 기존 PIN 과 충돌하지 않는다(= 바로 저장 가능)."""
    resp = await async_client.get(
        SUGGEST, params={"length": length}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["length"] == length
    pin = body["pin"]
    assert pin is not None and len(pin) == length and pin.isdigit()

    # 추천값이 정말 배정 가능한지 lookup 으로 교차 확인 — 두 경로가 같은 판정을 쓰는지.
    check = await async_client.get(LOOKUP, params={"pin": pin}, headers=admin_headers)
    assert check.json()["available"] is True


@pytest.mark.parametrize("bad_length", [3, 7])
async def test_suggest_rejects_out_of_range_length(
    async_client: AsyncClient, admin_headers: dict, bad_length: int
) -> None:
    """PIN 은 4~6자리만 존재한다 — 범위 밖 요청은 422."""
    resp = await async_client.get(
        SUGGEST, params={"length": bad_length}, headers=admin_headers
    )
    assert resp.status_code == 422


# ── delete (PIN 제거) ─────────────────────────────────────────────────────


async def test_delete_clears_pin_and_frees_the_number(
    async_client: AsyncClient, admin_headers: dict, pin_fixture: dict
) -> None:
    """PIN 제거 → 조회는 null, 그 번호는 다시 available."""
    sv_id = pin_fixture["sv_id"]
    resp = await async_client.delete(
        f"/api/v1/console/users/{sv_id}/clockin-pin", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["clockin_pin"] is None

    read = await async_client.get(
        f"/api/v1/console/users/{sv_id}/clockin-pin", headers=admin_headers
    )
    assert read.json()["clockin_pin"] is None

    freed = await async_client.get(
        LOOKUP, params={"pin": "1234"}, headers=admin_headers
    )
    assert freed.json()["available"] is True


async def test_delete_is_idempotent(
    async_client: AsyncClient, admin_headers: dict, pin_fixture: dict
) -> None:
    """이미 비어 있어도 200 — 도구에서 두 번 눌러도 에러가 아니다."""
    sv_id = pin_fixture["sv_id"]
    await async_client.delete(
        f"/api/v1/console/users/{sv_id}/clockin-pin", headers=admin_headers
    )
    again = await async_client.delete(
        f"/api/v1/console/users/{sv_id}/clockin-pin", headers=admin_headers
    )
    assert again.status_code == 200
    assert again.json()["clockin_pin"] is None


async def test_delete_requires_update_permission(
    async_client: AsyncClient, pin_fixture: dict
) -> None:
    """staff 계정은 남의 PIN 을 지울 수 없다."""
    login = await async_client.post(
        "/api/v1/app/auth/login", json={"username": "teststaff", "password": "1234"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    resp = await async_client.delete(
        f"/api/v1/console/users/{pin_fixture['sv_id']}/clockin-pin", headers=headers
    )
    assert resp.status_code == 403


async def test_delete_rejects_other_org_user(
    async_client: AsyncClient, admin_headers: dict
) -> None:
    """org 밖 user_id 는 404 — 도구를 통한 org 넘나들기 차단."""
    import uuid

    resp = await async_client.delete(
        f"/api/v1/console/users/{uuid.uuid4()}/clockin-pin", headers=admin_headers
    )
    assert resp.status_code == 404
