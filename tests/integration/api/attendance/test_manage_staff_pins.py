"""Integration — HTMA manage 모드 직원 PIN 조회/변경.

가장 중요한 두 축은 happy path 가 아니라 **가드**다.

  1. 권한: manage 세션 진입 문턱은 SV+ 인데 PIN 문턱은 GM+ 기본(`clockin_pin:*`).
     세션 토큰만으로 열리면 SV 가 부하 직원 PIN 을 전부 보게 된다.
  2. 스코프: 대상 직원이 이 기기의 매장 소속인지. 빠지면 org 전체 PIN 이 키오스크에서 열린다.

두 회귀는 조용히 일어나고(에러 없이 데이터만 새어나감) 사고 크기는 크다.
"""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text

from app.database import async_session
from app.models.clockin_pin_audit import ClockinPinAudit
from app.models.user_store import UserStore

pytestmark = pytest.mark.asyncio


async def _ensure_user_store(user_id: UUID, store_id: UUID, *, is_manager: bool) -> None:
    async with async_session() as db:
        existing = (
            await db.execute(
                select(UserStore).where(
                    UserStore.user_id == user_id,
                    UserStore.store_id == store_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(UserStore(user_id=user_id, store_id=store_id, is_manager=is_manager))
        elif is_manager and not existing.is_manager:
            existing.is_manager = True
        await db.commit()


@pytest_asyncio.fixture
async def gm_manage_headers(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_users: dict,
    test_store_id: UUID,
) -> dict:
    """GM(=clockin_pin permission 보유) 으로 연 manage 세션."""
    gm = test_users["testgm"]
    await _ensure_user_store(gm["id"], test_store_id, is_manager=True)
    resp = await async_client.post(
        "/api/v1/attendance/manage/session",
        headers=device_auth_headers,
        json={"pin": gm["clockin_pin"]},
    )
    assert resp.status_code == 201, resp.text
    return {**device_auth_headers, "X-Manage-Session": resp.json()["manage_token"]}


@pytest_asyncio.fixture
async def sv_manage_headers(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_users: dict,
    test_store_id: UUID,
) -> dict:
    """SV(=PIN permission 없음) 으로 연 manage 세션 — 진입 자체는 성공해야 한다."""
    sv = test_users["testsv"]
    await _ensure_user_store(sv["id"], test_store_id, is_manager=True)
    resp = await async_client.post(
        "/api/v1/attendance/manage/session",
        headers=device_auth_headers,
        json={"pin": sv["clockin_pin"]},
    )
    assert resp.status_code == 201, resp.text
    return {**device_auth_headers, "X-Manage-Session": resp.json()["manage_token"]}


@pytest_asyncio.fixture
async def staff_in_store(test_user: dict, test_store_id: UUID) -> dict:
    await _ensure_user_store(test_user["id"], test_store_id, is_manager=False)
    return test_user


# ── 권한 가드 ──────────────────────────────────────────────


async def test_session_response_exposes_pin_flags_for_gm(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_users: dict,
    test_store_id: UUID,
) -> None:
    """GM 세션은 PIN 메뉴 플래그가 켜진 채로 발급된다."""
    gm = test_users["testgm"]
    await _ensure_user_store(gm["id"], test_store_id, is_manager=True)
    resp = await async_client.post(
        "/api/v1/attendance/manage/session",
        headers=device_auth_headers,
        json={"pin": gm["clockin_pin"]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["can_read_pins"] is True
    assert resp.json()["can_update_pins"] is True


async def test_session_response_hides_pin_flags_for_sv(
    sv_manage_headers: dict,
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_users: dict,
) -> None:
    """SV 세션은 진입은 되지만 PIN 플래그가 꺼져 있다 — 앱이 메뉴를 숨기는 근거."""
    sv = test_users["testsv"]
    resp = await async_client.post(
        "/api/v1/attendance/manage/session",
        headers=device_auth_headers,
        json={"pin": sv["clockin_pin"]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["can_read_pins"] is False
    assert resp.json()["can_update_pins"] is False


async def test_sv_cannot_list_staff_pins(
    async_client: AsyncClient, sv_manage_headers: dict
) -> None:
    """**핵심 회귀 방지** — manage 세션이 있어도 SV 는 PIN 목록을 못 본다."""
    resp = await async_client.get(
        "/api/v1/attendance/manage/staff-pins", headers=sv_manage_headers
    )
    assert resp.status_code == 403, resp.text


async def test_sv_cannot_reveal_pin(
    async_client: AsyncClient, sv_manage_headers: dict, staff_in_store: dict
) -> None:
    resp = await async_client.get(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}/reveal",
        headers=sv_manage_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_sv_cannot_update_pin(
    async_client: AsyncClient,
    sv_manage_headers: dict,
    staff_in_store: dict,
    restore_pins: None,
) -> None:
    resp = await async_client.patch(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}",
        headers=sv_manage_headers,
        json={"clockin_pin": "987654"},
    )
    assert resp.status_code == 403, resp.text


async def test_without_manage_session_is_401(
    async_client: AsyncClient, device_auth_headers: dict
) -> None:
    """device token 만으로는 접근 불가."""
    resp = await async_client.get(
        "/api/v1/attendance/manage/staff-pins", headers=device_auth_headers
    )
    assert resp.status_code == 401, resp.text


# ── 스코프 가드 ────────────────────────────────────────────


async def test_reveal_rejects_user_outside_store(
    async_client: AsyncClient,
    gm_manage_headers: dict,
    test_users: dict,
    test_store_id: UUID,
    second_store_id: UUID,
) -> None:
    """다른 매장에만 배정된 직원은 404 — org 전체 노출 회귀 방지."""
    outsider = test_users["testadmin"]
    async with async_session() as db:
        await db.execute(
            text(
                "DELETE FROM user_stores WHERE user_id=:uid AND store_id=:sid"
            ),
            {"uid": str(outsider["id"]), "sid": str(test_store_id)},
        )
        await db.commit()
    await _ensure_user_store(outsider["id"], second_store_id, is_manager=False)

    resp = await async_client.get(
        f"/api/v1/attendance/manage/staff-pins/{outsider['id']}/reveal",
        headers=gm_manage_headers,
    )
    assert resp.status_code == 404, resp.text


# ── 목록 ───────────────────────────────────────────────────


async def test_list_does_not_leak_plaintext_pin(
    async_client: AsyncClient, gm_manage_headers: dict, staff_in_store: dict
) -> None:
    """목록 응답 본문 어디에도 평문 PIN 이 없어야 한다 (has_pin 만)."""
    resp = await async_client.get(
        "/api/v1/attendance/manage/staff-pins", headers=gm_manage_headers
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert rows, "매장 직원이 최소 1명은 나와야 한다"
    assert all("clockin_pin" not in row for row in rows)
    target = next(r for r in rows if r["user_id"] == str(staff_in_store["id"]))
    assert target["has_pin"] is True
    assert staff_in_store["clockin_pin"] not in resp.text


async def test_list_search_filters_by_name(
    async_client: AsyncClient, gm_manage_headers: dict, staff_in_store: dict
) -> None:
    """검색어로 좁혀진다."""
    resp = await async_client.get(
        "/api/v1/attendance/manage/staff-pins",
        headers=gm_manage_headers,
        params={"q": "teststaff"},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert rows
    assert all("teststaff" in (r["full_name"] + (r["employee_no"] or "")).lower()
               or r["user_id"] == str(staff_in_store["id"]) for r in rows)


async def test_list_sorts_today_workers_first(
    async_client: AsyncClient,
    gm_manage_headers: dict,
    staff_in_store: dict,
    test_schedule: UUID,
) -> None:
    """오늘 스케줄이 있는 사람이 위로 — 필터가 아니라 정렬."""
    resp = await async_client.get(
        "/api/v1/attendance/manage/staff-pins", headers=gm_manage_headers
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    flags = [r["works_today"] for r in rows]
    # True 구간이 앞쪽에 몰려 있어야 한다 (False 뒤에 True 가 오면 실패)
    assert flags == sorted(flags, reverse=True), flags


# ── 변경 ───────────────────────────────────────────────────


async def test_update_pin_accepts_four_digits(
    async_client: AsyncClient,
    gm_manage_headers: dict,
    staff_in_store: dict,
    restore_pins: None,
) -> None:
    """4자리 허용 (D3)."""
    resp = await async_client.patch(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}",
        headers=gm_manage_headers,
        json={"clockin_pin": "4821"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["clockin_pin"] == "4821"


@pytest.mark.parametrize("bad", ["123", "1234567", "12a4", ""])
async def test_update_pin_rejects_bad_length(
    async_client: AsyncClient,
    gm_manage_headers: dict,
    staff_in_store: dict,
    restore_pins: None,
    bad: str,
) -> None:
    """4~6자리 숫자 밖은 422."""
    resp = await async_client.patch(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}",
        headers=gm_manage_headers,
        json={"clockin_pin": bad},
    )
    assert resp.status_code == 422, resp.text


async def test_update_pin_rejects_prefix_conflict(
    async_client: AsyncClient,
    gm_manage_headers: dict,
    staff_in_store: dict,
    test_users: dict,
    restore_pins: None,
) -> None:
    """다른 직원 PIN 을 prefix 로 갖는 값은 409."""
    async with async_session() as db:
        await db.execute(
            text("UPDATE users SET clockin_pin='4455' WHERE id=:id"),
            {"id": str(test_users["testsv"]["id"])},
        )
        await db.commit()

    resp = await async_client.patch(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}",
        headers=gm_manage_headers,
        json={"clockin_pin": "445566"},
    )
    assert resp.status_code == 409, resp.text


# ── 409 detail 계약 (pin_conflict) ─────────────────────────


async def _set_pin(user_id: UUID, pin: str) -> None:
    async with async_session() as db:
        await db.execute(
            text("UPDATE users SET clockin_pin=:pin WHERE id=:id"),
            {"pin": pin, "id": str(user_id)},
        )
        await db.commit()


async def test_update_pin_exact_conflict_same_store_detail(
    async_client: AsyncClient,
    gm_manage_headers: dict,
    staff_in_store: dict,
    test_users: dict,
    test_store_id: UUID,
    restore_pins: None,
) -> None:
    """같은 매장 직원과 정확히 같은 PIN → exact + other_store=False."""
    sv = test_users["testsv"]
    await _ensure_user_store(sv["id"], test_store_id, is_manager=False)
    await _set_pin(sv["id"], "7717")

    resp = await async_client.patch(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}",
        headers=gm_manage_headers,
        json={"clockin_pin": "7717"},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "pin_conflict"
    assert detail["reason"] == "exact"
    assert detail["other_store"] is False
    assert detail["message"] == "This PIN is already in use by another employee."
    # 충돌 상대의 정체(이름)는 응답 어디에도 없다
    assert sv["full_name"] not in resp.text


async def test_update_pin_exact_conflict_other_store_detail(
    async_client: AsyncClient,
    gm_manage_headers: dict,
    staff_in_store: dict,
    test_users: dict,
    test_store_id: UUID,
    restore_pins: None,
) -> None:
    """기기 매장 밖 직원과 충돌 → other_store=True + 다른 매장 안내 메시지."""
    outsider = test_users["testadmin"]
    # 기기 매장 배정 제거 — '다른 매장 소속' 상태를 만든다 (종료 시 복원)
    async with async_session() as db:
        had_row = (
            await db.execute(
                select(UserStore).where(
                    UserStore.user_id == outsider["id"],
                    UserStore.store_id == test_store_id,
                )
            )
        ).scalar_one_or_none() is not None
        await db.execute(
            text(
                "DELETE FROM user_stores WHERE user_id=:uid AND store_id=:sid"
            ),
            {"uid": str(outsider["id"]), "sid": str(test_store_id)},
        )
        await db.commit()
    await _set_pin(outsider["id"], "8828")

    try:
        resp = await async_client.patch(
            f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}",
            headers=gm_manage_headers,
            json={"clockin_pin": "8828"},
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "pin_conflict"
        assert detail["reason"] == "exact"
        assert detail["other_store"] is True
        assert detail["message"] == (
            "This PIN is already in use by an employee at another store."
        )
        # 충돌 상대의 정체(이름)는 응답 어디에도 없다
        assert outsider["full_name"] not in resp.text
    finally:
        if had_row:
            await _ensure_user_store(
                outsider["id"], test_store_id, is_manager=True
            )


async def test_update_pin_prefix_conflict_detail(
    async_client: AsyncClient,
    gm_manage_headers: dict,
    staff_in_store: dict,
    test_users: dict,
    test_store_id: UUID,
    restore_pins: None,
) -> None:
    """prefix 충돌 → reason=prefix + 겹침 안내 (충돌 PIN 값은 절대 비노출)."""
    sv = test_users["testsv"]
    await _ensure_user_store(sv["id"], test_store_id, is_manager=False)
    await _set_pin(sv["id"], "6644")

    resp = await async_client.patch(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}",
        headers=gm_manage_headers,
        json={"clockin_pin": "664422"},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "pin_conflict"
    assert detail["reason"] == "prefix"
    assert detail["other_store"] is False
    assert detail["message"] == (
        "This PIN overlaps with another employee's PIN "
        "(numbers that start the same)."
    )
    # 충돌 상대 PIN(제출값의 앞자리)이 응답 본문에 없다 — echo 금지
    assert "6644" not in resp.text


async def test_regenerate_changes_pin(
    async_client: AsyncClient,
    gm_manage_headers: dict,
    staff_in_store: dict,
    restore_pins: None,
) -> None:
    """재발급하면 6자리 새 값이 나온다."""
    before = staff_in_store["clockin_pin"]
    resp = await async_client.post(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}/regenerate",
        headers=gm_manage_headers,
    )
    assert resp.status_code == 200, resp.text
    after = resp.json()["clockin_pin"]
    assert after != before
    assert len(after) == 6 and after.isdigit()


# ── 감사 로그 ──────────────────────────────────────────────


async def _audit_count(target_id: UUID, action: str) -> int:
    async with async_session() as db:
        rows = await db.execute(
            select(ClockinPinAudit).where(
                ClockinPinAudit.target_user_id == target_id,
                ClockinPinAudit.action == action,
            )
        )
        return len(list(rows.scalars().all()))


async def test_reveal_records_audit(
    async_client: AsyncClient, gm_manage_headers: dict, staff_in_store: dict
) -> None:
    """평문을 실제로 연 순간이 기록된다 — 유출 경로 추적의 유일한 근거."""
    before = await _audit_count(staff_in_store["id"], "reveal")
    resp = await async_client.get(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}/reveal",
        headers=gm_manage_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["clockin_pin"] == staff_in_store["clockin_pin"]
    assert await _audit_count(staff_in_store["id"], "reveal") == before + 1


async def test_list_does_not_record_audit(
    async_client: AsyncClient, gm_manage_headers: dict, staff_in_store: dict
) -> None:
    """목록은 마스킹 상태라 기록 대상이 아니다 — 로그가 의미를 잃지 않게."""
    before = await _audit_count(staff_in_store["id"], "reveal")
    await async_client.get(
        "/api/v1/attendance/manage/staff-pins", headers=gm_manage_headers
    )
    assert await _audit_count(staff_in_store["id"], "reveal") == before


async def test_update_records_audit(
    async_client: AsyncClient,
    gm_manage_headers: dict,
    staff_in_store: dict,
    restore_pins: None,
) -> None:
    before = await _audit_count(staff_in_store["id"], "update")
    resp = await async_client.patch(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}",
        headers=gm_manage_headers,
        json={"clockin_pin": "7391"},
    )
    assert resp.status_code == 200, resp.text
    assert await _audit_count(staff_in_store["id"], "update") == before + 1


async def test_regenerate_records_audit(
    async_client: AsyncClient,
    gm_manage_headers: dict,
    staff_in_store: dict,
    restore_pins: None,
) -> None:
    before = await _audit_count(staff_in_store["id"], "regenerate")
    resp = await async_client.post(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}/regenerate",
        headers=gm_manage_headers,
    )
    assert resp.status_code == 200, resp.text
    assert await _audit_count(staff_in_store["id"], "regenerate") == before + 1


async def test_failed_update_leaves_no_audit(
    async_client: AsyncClient,
    gm_manage_headers: dict,
    staff_in_store: dict,
    test_users: dict,
    restore_pins: None,
) -> None:
    """409 로 막힌 변경은 감사 로그도 남기지 않는다 (롤백 확인)."""
    async with async_session() as db:
        await db.execute(
            text("UPDATE users SET clockin_pin='5511' WHERE id=:id"),
            {"id": str(test_users["testsv"]["id"])},
        )
        await db.commit()

    before = await _audit_count(staff_in_store["id"], "update")
    resp = await async_client.patch(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}",
        headers=gm_manage_headers,
        json={"clockin_pin": "551122"},
    )
    assert resp.status_code == 409, resp.text
    assert await _audit_count(staff_in_store["id"], "update") == before


# ── meta 페이로드 ──────────────────────────────────────────


async def _latest_audit(target_id: UUID, action: str) -> ClockinPinAudit | None:
    async with async_session() as db:
        rows = await db.execute(
            select(ClockinPinAudit)
            .where(
                ClockinPinAudit.target_user_id == target_id,
                ClockinPinAudit.action == action,
            )
            .order_by(ClockinPinAudit.created_at.desc())
            .limit(1)
        )
        return rows.scalar_one_or_none()


async def test_audit_meta_never_contains_the_pin(
    async_client: AsyncClient,
    gm_manage_headers: dict,
    staff_in_store: dict,
    restore_pins: None,
) -> None:
    """**핵심** — 감사 로그가 PIN 사본이 되면 안 된다. meta 에 값이 새면 실패."""
    await async_client.patch(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}",
        headers=gm_manage_headers,
        json={"clockin_pin": "6142"},
    )
    row = await _latest_audit(staff_in_store["id"], "update")
    assert row is not None
    assert "6142" not in str(row.meta)


async def test_update_meta_records_pin_length(
    async_client: AsyncClient,
    gm_manage_headers: dict,
    staff_in_store: dict,
    restore_pins: None,
) -> None:
    """길이 변경 시점을 짚을 수 있어야 한다 — 4자리로 바뀐 뒤 문의가 들어올 때."""
    resp = await async_client.patch(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}",
        headers=gm_manage_headers,
        json={"clockin_pin": "6143"},
    )
    assert resp.status_code == 200, resp.text
    row = await _latest_audit(staff_in_store["id"], "update")
    assert row is not None
    assert row.meta["pin_length"] == 4
    assert row.meta["source"] == "kiosk_manage"


async def test_regenerate_meta_records_length_six(
    async_client: AsyncClient,
    gm_manage_headers: dict,
    staff_in_store: dict,
    restore_pins: None,
) -> None:
    resp = await async_client.post(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}/regenerate",
        headers=gm_manage_headers,
    )
    assert resp.status_code == 200, resp.text
    row = await _latest_audit(staff_in_store["id"], "regenerate")
    assert row is not None
    assert row.meta["pin_length"] == 6


async def test_reveal_meta_records_source_only(
    async_client: AsyncClient, gm_manage_headers: dict, staff_in_store: dict
) -> None:
    """조회는 기록할 값 자체가 없다 — source 만."""
    await async_client.get(
        f"/api/v1/attendance/manage/staff-pins/{staff_in_store['id']}/reveal",
        headers=gm_manage_headers,
    )
    row = await _latest_audit(staff_in_store["id"], "reveal")
    assert row is not None
    assert row.meta == {"source": "kiosk_manage"}
    assert "pin_length" not in row.meta
