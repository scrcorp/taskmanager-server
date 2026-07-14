"""근무가능시간(Work Availability) API 통합 테스트.

실제 seed DB 사용(롤백 없음) — 생성 행은 정리 픽스처로 purge.
커버: 3상태 저장/조회, 검증(범위·overnight 허용·동일시각 거부·5분그리드), 이력,
권한(read/manage), 공유매장 IDOR, 앱 셀프 최초1회 정책(미설정→편집가능/설정후→403), 요일 0=Sun.
"""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.models.availability import StaffAvailability, StaffAvailabilityHistory
from app.models.permission import Permission, RolePermission
from app.models.user import User
from app.models.user_store import UserStore
from app.utils.jwt import create_access_token


async def _login(username: str) -> str:
    async with async_session() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one()
        return create_access_token({"sub": str(user.id), "org": str(user.organization_id)})


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _clear_user_availability(user_id: UUID) -> None:
    """한 유저의 staff_availability + history 행을 모두 삭제 → '미설정' 상태 강제.

    실 DB(롤백 없음)라 최초1회 정책 테스트를 결정적으로 만들기 위한 헬퍼.
    """
    async with async_session() as db:
        await db.execute(
            delete(StaffAvailabilityHistory).where(StaffAvailabilityHistory.user_id == user_id)
        )
        await db.execute(
            delete(StaffAvailability).where(StaffAvailability.user_id == user_id)
        )
        await db.commit()


async def _delete_availability_rows_only(user_id: UUID) -> None:
    """staff_availability 행만 삭제(history 는 남김).

    매니저가 주 전체를 Off 로 저장 → save_week 가 모든 행을 DELETE 하는 상황을 흉내낸다.
    history(append-only) 는 그대로라 최초1회 게이트가 유지되어야 함.
    """
    async with async_session() as db:
        await db.execute(
            delete(StaffAvailability).where(StaffAvailability.user_id == user_id)
        )
        await db.commit()


# ── 정리: availability 테이블 purge (테스트 전후) ────────────
@pytest_asyncio.fixture(autouse=True)
async def _clean_availability():
    async def purge():
        async with async_session() as db:
            await db.execute(delete(StaffAvailabilityHistory))
            await db.execute(delete(StaffAvailability))
            # availability role-permission grants 도 초기화 → 음성(403) 테스트 결정성 보장
            perm_ids = list((await db.execute(select(Permission.id).where(
                Permission.code.in_(["availability:read", "availability:manage"])))).scalars().all())
            if perm_ids:
                await db.execute(delete(RolePermission).where(RolePermission.permission_id.in_(perm_ids)))
            await db.commit()
    await purge()
    yield
    await purge()


# ── 권한 부여 (startup 미실행이라 role_permissions 수동 시드) ──
@pytest_asyncio.fixture
async def availability_perms(seed_roles: dict[str, UUID], _clean_availability):
    # _clean_availability 뒤에 실행(순서 강제) — purge 후 grant.
    # staff 는 절대 부여하지 않음(설계). GM/SV 만. 실DB라 teardown 에서 grant 정리.
    codes = ["availability:read", "availability:manage"]
    created: list[tuple[UUID, UUID]] = []
    async with async_session() as db:
        perms: dict[str, UUID] = {}
        for code in codes:
            p = (await db.execute(select(Permission).where(Permission.code == code))).scalar_one_or_none()
            if p is None:
                resource, action = code.split(":")
                p = Permission(code=code, resource=resource, action=action)
                db.add(p)
                await db.flush()
            perms[code] = p.id
        for role_name in ("general_manager", "supervisor"):
            role_id = seed_roles[role_name]
            for code in codes:
                exists = (await db.execute(select(RolePermission).where(
                    RolePermission.role_id == role_id,
                    RolePermission.permission_id == perms[code],
                ))).scalar_one_or_none()
                if exists is None:
                    db.add(RolePermission(role_id=role_id, permission_id=perms[code]))
                    created.append((role_id, perms[code]))
        await db.commit()
    yield
    async with async_session() as db:
        for role_id, perm_id in created:
            await db.execute(delete(RolePermission).where(
                RolePermission.role_id == role_id,
                RolePermission.permission_id == perm_id,
            ))
        await db.commit()


# ── 공유매장 IDOR 셋업: gm+sv=test_store, staff=second_store ──
@pytest_asyncio.fixture
async def idor_stores(test_users: dict, test_store_id: UUID, second_store_id: UUID):
    created: list[UUID] = []

    async def assign(user_id: UUID, store_id: UUID, is_manager: bool) -> None:
        async with async_session() as db:
            row = (await db.execute(select(UserStore).where(
                UserStore.user_id == user_id, UserStore.store_id == store_id,
            ))).scalar_one_or_none()
            if row is None:
                us = UserStore(user_id=user_id, store_id=store_id,
                               is_manager=is_manager, is_work_assignment=True)
                db.add(us)
                await db.flush()
                created.append(us.id)
                await db.commit()

    gm = test_users["testgm"]["id"]
    sv = test_users["testsv"]["id"]
    staff = test_users["teststaff"]["id"]
    await assign(gm, test_store_id, True)
    await assign(sv, test_store_id, False)
    await assign(staff, second_store_id, False)
    yield {"gm": gm, "sv": sv, "staff": staff}
    async with async_session() as db:
        if created:
            await db.execute(delete(UserStore).where(UserStore.id.in_(created)))
            await db.commit()


# ─────────────────────────── 저장/조회/이력 ───────────────────────────
@pytest.mark.asyncio
async def test_admin_save_and_get_week(async_client: AsyncClient, test_users: dict):
    staff_id = test_users["teststaff"]["id"]
    token = await _login("testadmin")  # super_owner — permission/IDOR bypass
    payload = {"days": [
        {"day_of_week": 0, "state": "full"},                                     # Sun full
        {"day_of_week": 1, "state": "range", "start_time": "09:00", "end_time": "14:30"},
        {"day_of_week": 3, "state": "off"},                                      # explicit off (no row)
    ]}
    r = await async_client.put(f"/api/v1/console/availability/staff/{staff_id}",
                               headers=_h(token), json=payload)
    assert r.status_code == 200, r.text
    days = {d["day_of_week"]: d for d in r.json()["days"]}
    assert len(r.json()["days"]) == 7            # 항상 7일
    assert days[0]["state"] == "full"            # 0=Sun
    assert days[1]["state"] == "range" and days[1]["start_time"] == "09:00"
    assert days[3]["state"] == "off"
    assert days[6]["state"] == "off"

    # GET detail + history
    g = await async_client.get(f"/api/v1/console/availability/staff/{staff_id}", headers=_h(token))
    assert g.status_code == 200, g.text
    body = g.json()
    assert {d["day_of_week"]: d["state"] for d in body["member"]["days"]}[0] == "full"
    assert len(body["history"]) >= 2             # Sun + Mon 변경 이력


@pytest.mark.asyncio
async def test_bulk_list(async_client: AsyncClient):
    token = await _login("testadmin")
    r = await async_client.get("/api/v1/console/availability", headers=_h(token))
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


# ─────────────────────────── 검증 ───────────────────────────
@pytest.mark.asyncio
async def test_range_requires_times(async_client: AsyncClient, test_users: dict):
    token = await _login("testadmin")
    staff_id = test_users["teststaff"]["id"]
    r = await async_client.put(f"/api/v1/console/availability/staff/{staff_id}", headers=_h(token),
                               json={"days": [{"day_of_week": 1, "state": "range"}]})
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_overnight_allowed(async_client: AsyncClient, test_users: dict):
    token = await _login("testadmin")
    staff_id = test_users["teststaff"]["id"]
    r = await async_client.put(f"/api/v1/console/availability/staff/{staff_id}", headers=_h(token),
                               json={"days": [{"day_of_week": 1, "state": "range",
                                               "start_time": "17:00", "end_time": "02:00"}]})
    assert r.status_code == 200, r.text  # overnight (end < start) now allowed
    days = {d["day_of_week"]: d for d in r.json()["days"]}
    assert days[1]["state"] == "range"
    assert days[1]["start_time"] == "17:00" and days[1]["end_time"] == "02:00"


@pytest.mark.asyncio
async def test_equal_times_rejected(async_client: AsyncClient, test_users: dict):
    token = await _login("testadmin")
    staff_id = test_users["teststaff"]["id"]
    r = await async_client.put(f"/api/v1/console/availability/staff/{staff_id}", headers=_h(token),
                               json={"days": [{"day_of_week": 1, "state": "range",
                                               "start_time": "09:00", "end_time": "09:00"}]})
    assert r.status_code == 422, r.text  # start == end is invalid


@pytest.mark.asyncio
async def test_5min_grid_allowed(async_client: AsyncClient, test_users: dict):
    token = await _login("testadmin")
    staff_id = test_users["teststaff"]["id"]
    r = await async_client.put(f"/api/v1/console/availability/staff/{staff_id}", headers=_h(token),
                               json={"days": [{"day_of_week": 1, "state": "range",
                                               "start_time": "09:05", "end_time": "14:35"}]})
    assert r.status_code == 200, r.text  # 5-minute grid now valid
    days = {d["day_of_week"]: d for d in r.json()["days"]}
    assert days[1]["start_time"] == "09:05" and days[1]["end_time"] == "14:35"


@pytest.mark.asyncio
async def test_non_5min_grid_rejected(async_client: AsyncClient, test_users: dict):
    token = await _login("testadmin")
    staff_id = test_users["teststaff"]["id"]
    r = await async_client.put(f"/api/v1/console/availability/staff/{staff_id}", headers=_h(token),
                               json={"days": [{"day_of_week": 1, "state": "range",
                                               "start_time": "09:07", "end_time": "14:30"}]})
    assert r.status_code == 422, r.text  # 09:07 not on 5-minute boundary


# ─────────────────────────── 권한 ───────────────────────────
@pytest.mark.asyncio
async def test_sv_read_forbidden_without_grant(async_client: AsyncClient):
    token = await _login("testsv")  # no availability_perms
    r = await async_client.get("/api/v1/console/availability", headers=_h(token))
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_sv_read_ok_with_grant(async_client: AsyncClient, availability_perms):
    token = await _login("testsv")
    r = await async_client.get("/api/v1/console/availability", headers=_h(token))
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_staff_manage_forbidden(async_client: AsyncClient, test_users: dict):
    token = await _login("teststaff")  # staff never gets availability:manage
    staff_id = test_users["teststaff"]["id"]
    r = await async_client.put(f"/api/v1/console/availability/staff/{staff_id}", headers=_h(token),
                               json={"days": [{"day_of_week": 0, "state": "full"}]})
    assert r.status_code == 403, r.text


# ─────────────────────────── 공유매장 IDOR ───────────────────────────
@pytest.mark.asyncio
async def test_idor_shared_store_allows(async_client: AsyncClient, availability_perms, idor_stores):
    token = await _login("testgm")
    r = await async_client.put(f"/api/v1/console/availability/staff/{idor_stores['sv']}",
                               headers=_h(token), json={"days": [{"day_of_week": 0, "state": "full"}]})
    assert r.status_code == 200, r.text  # gm & sv share test_store


@pytest.mark.asyncio
async def test_idor_no_shared_store_forbids(async_client: AsyncClient, availability_perms, idor_stores):
    token = await _login("testgm")
    r = await async_client.put(f"/api/v1/console/availability/staff/{idor_stores['staff']}",
                               headers=_h(token), json={"days": [{"day_of_week": 0, "state": "full"}]})
    assert r.status_code == 403, r.text  # staff only in second_store


# ─────────────────────────── 앱 셀프 최초1회 정책 ───────────────────────────
# 당분간 정책: 미설정(최초 1회)일 때만 본인 편집 가능. 설정 후에는 매니저만 변경.
@pytest.mark.asyncio
async def test_app_get_my_availability_unset(async_client: AsyncClient, test_users: dict):
    staff_id = test_users["teststaff"]["id"]
    await _clear_user_availability(staff_id)  # 미설정 강제
    token = await _login("teststaff")
    r = await async_client.get("/api/v1/app/my/availability", headers=_h(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["can_edit"] is True  # 미설정 → 최초 1회 편집 가능
    assert len(body["days"]) == 7
    assert all(d["state"] == "off" for d in body["days"])
    await _clear_user_availability(staff_id)  # 정리


@pytest.mark.asyncio
async def test_app_first_time_set_then_locked(async_client: AsyncClient, test_users: dict):
    staff_id = test_users["teststaff"]["id"]
    await _clear_user_availability(staff_id)  # 미설정 강제
    token = await _login("teststaff")

    # 최초 1회 PUT → 성공, 이후 편집 불가(can_edit False)
    r = await async_client.put("/api/v1/app/my/availability", headers=_h(token),
                               json={"days": [{"day_of_week": 2, "state": "range",
                                               "start_time": "10:00", "end_time": "16:00"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["can_edit"] is False  # 설정 완료 → 잠김
    days = {d["day_of_week"]: d for d in body["days"]}
    assert days[2]["state"] == "range" and days[2]["end_time"] == "16:00"

    # GET 재확인 → 여전히 잠김
    g = await async_client.get("/api/v1/app/my/availability", headers=_h(token))
    assert g.status_code == 200, g.text
    assert g.json()["can_edit"] is False

    # 이미 설정됨 → 재-PUT 은 403 (매니저에게 문의)
    r2 = await async_client.put("/api/v1/app/my/availability", headers=_h(token),
                                json={"days": [{"day_of_week": 3, "state": "full"}]})
    assert r2.status_code == 403, r2.text
    assert "manager" in r2.text.lower()

    await _clear_user_availability(staff_id)  # 정리 → 다른 테스트 영향 없음


@pytest.mark.asyncio
async def test_app_gate_held_by_history_after_rows_wiped(
    async_client: AsyncClient, test_users: dict
):
    """FIX 1: 게이트는 history(append-only)로 판정 — 행이 지워져도 다시 열리지 않는다.

    스태프 최초1회 셀프 저장(history 생성) → 매니저가 주 전체 Off 로 저장하는 상황을
    흉내내어 staff_availability 행만 DELETE(history 유지) → GET can_edit=False,
    재-PUT 은 여전히 403. (기존 버그: updated_at 파생 게이트는 행 삭제 시 재개방됐음)
    """
    staff_id = test_users["teststaff"]["id"]
    await _clear_user_availability(staff_id)  # 미설정 강제
    token = await _login("teststaff")

    # 최초 1회 PUT → 성공, history 생성
    r = await async_client.put("/api/v1/app/my/availability", headers=_h(token),
                               json={"days": [{"day_of_week": 2, "state": "range",
                                               "start_time": "10:00", "end_time": "16:00"}]})
    assert r.status_code == 200, r.text
    assert r.json()["can_edit"] is False

    # 매니저가 주 전체 Off 저장을 흉내 → staff_availability 행만 삭제(history 유지)
    await _delete_availability_rows_only(staff_id)

    # 행이 없어도 history 존재 → 여전히 잠김
    g = await async_client.get("/api/v1/app/my/availability", headers=_h(token))
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["can_edit"] is False  # history 로 게이트 유지 (행 삭제로 열리지 않음)
    assert all(d["state"] == "off" for d in body["days"])  # 행이 없으니 전부 off 로 표시

    # 재-PUT 도 여전히 403 (게이트는 행이 아니라 history 가 지킨다)
    r2 = await async_client.put("/api/v1/app/my/availability", headers=_h(token),
                                json={"days": [{"day_of_week": 4, "state": "full"}]})
    assert r2.status_code == 403, r2.text
    assert "manager" in r2.text.lower()

    await _clear_user_availability(staff_id)  # 정리 → 다른 테스트 영향 없음
