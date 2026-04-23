"""Attendance Device — 등록/인증 흐름 테스트."""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance_device import AttendanceDevice


pytestmark = pytest.mark.asyncio


async def test_register_with_valid_access_code(
    async_client: AsyncClient,
    attendance_access_code: str,
    _session_created_device_ids: list,
) -> None:
    resp = await async_client.post(
        "/api/v1/attendance/register",
        json={"access_code": attendance_access_code, "fingerprint": "ua-test"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["token"]
    assert UUID(body["device_id"])
    assert body["device_name"].startswith("Terminal-")
    _session_created_device_ids.append(UUID(body["device_id"]))


async def test_register_with_invalid_access_code(
    async_client: AsyncClient,
) -> None:
    resp = await async_client.post(
        "/api/v1/attendance/register",
        json={"access_code": "WRONG1", "fingerprint": "ua-test"},
    )
    assert resp.status_code == 401
    assert "Invalid access code" in resp.json()["detail"]


async def test_register_returns_store_id_null(
    async_client: AsyncClient,
    attendance_access_code: str,
    _session_created_device_ids: list,
) -> None:
    resp = await async_client.post(
        "/api/v1/attendance/register",
        json={"access_code": attendance_access_code},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["store_id"] is None
    _session_created_device_ids.append(UUID(body["device_id"]))


async def test_get_me_without_token(async_client: AsyncClient) -> None:
    resp = await async_client.get("/api/v1/attendance/me")
    # HTTPBearer with auto_error=False → credentials is None → service raises 401
    assert resp.status_code in (401, 403)


async def test_get_me_with_revoked_token(
    async_client: AsyncClient,
    attendance_access_code: str,
    db: AsyncSession,
    _session_created_device_ids: list,
) -> None:
    # register → revoke manually in DB → subsequent /me must be 401
    resp = await async_client.post(
        "/api/v1/attendance/register",
        json={"access_code": attendance_access_code},
    )
    body = resp.json()
    token = body["token"]
    device_id = UUID(body["device_id"])
    _session_created_device_ids.append(device_id)

    # revoke via DELETE /api/v1/attendance/me
    resp_del = await async_client.delete(
        "/api/v1/attendance/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp_del.status_code == 204

    resp_me = await async_client.get(
        "/api/v1/attendance/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp_me.status_code == 401


async def test_assign_store_updates_device(
    async_client: AsyncClient,
    unassigned_device_token: str,
    test_store_id: UUID,
) -> None:
    resp = await async_client.put(
        "/api/v1/attendance/store",
        headers={"Authorization": f"Bearer {unassigned_device_token}"},
        json={"store_id": str(test_store_id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["store_id"] == str(test_store_id)


async def test_device_me_includes_store_timezone_info(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_store_id: UUID,
) -> None:
    """GET /me 응답에 store_timezone, store_timezone_offset_minutes, work_date
    필드가 존재하며 매장이 지정된 상태에서는 값이 채워진다."""
    import re
    resp = await async_client.get("/api/v1/attendance/me", headers=device_auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 키 존재 확인
    assert "store_timezone" in body
    assert "store_timezone_offset_minutes" in body
    assert "work_date" in body
    # 매장 할당된 기기 → 값 채움
    assert body["store_id"] == str(test_store_id)
    # __attendance_test_store__ 은 UTC 로 고정
    assert body["store_timezone"] == "UTC"
    # UTC 오프셋은 0
    assert body["store_timezone_offset_minutes"] == 0
    # work_date 는 YYYY-MM-DD 형식
    assert isinstance(body["work_date"], str)
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", body["work_date"])


async def test_device_me_unassigned_store_has_null_tz_fields(
    async_client: AsyncClient,
    unassigned_device_token: str,
) -> None:
    """매장이 지정되지 않은 기기의 /me 응답은 tz/work_date 가 null 허용."""
    resp = await async_client.get(
        "/api/v1/attendance/me",
        headers={"Authorization": f"Bearer {unassigned_device_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["store_id"] is None
    assert body["store_timezone"] is None
    assert body["store_timezone_offset_minutes"] is None
    assert body["work_date"] is None


# ── Device name — store code 기반 순번 ────────────────────────────────


async def test_device_name_uses_store_code_on_assign(
    async_client: AsyncClient,
    attendance_access_code: str,
    db: AsyncSession,
    _session_created_device_ids: list,
) -> None:
    """store.code='NB' 인 매장에 기기 할당 → device_name='NB001'.
    두 번째 기기 할당 → 'NB002'."""
    from uuid import UUID as _UUID

    from app.database import async_session
    from app.models.attendance_device import AttendanceDevice
    from app.models.organization import Organization, Store
    from sqlalchemy import delete as _delete

    # 전용 테스트 매장 생성 (code='NB')
    async with async_session() as sess:
        org = (
            await sess.execute(
                select(Organization).order_by(Organization.created_at).limit(1)
            )
        ).scalar_one()
        existing = (
            await sess.execute(
                select(Store).where(
                    Store.organization_id == org.id,
                    Store.name == "__attendance_test_store_code_NB__",
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            store = Store(
                organization_id=org.id,
                name="__attendance_test_store_code_NB__",
                code="NB",
                timezone="UTC",
                day_start_time={"all": "00:00"},
            )
            sess.add(store)
            await sess.commit()
            await sess.refresh(store)
            store_id = store.id
        else:
            existing.code = "NB"
            await sess.commit()
            store_id = existing.id

        # 이 매장에 기존에 달라붙어 있는 기기들을 모두 revoke 처리해서 카운트 초기화
        await sess.execute(
            _delete(AttendanceDevice).where(AttendanceDevice.store_id == store_id)
        )
        await sess.commit()

    try:
        # 첫 기기 등록 + assign
        r1 = await async_client.post(
            "/api/v1/attendance/register",
            json={"access_code": attendance_access_code},
        )
        assert r1.status_code == 201, r1.text
        token1 = r1.json()["token"]
        _session_created_device_ids.append(_UUID(r1.json()["device_id"]))
        a1 = await async_client.put(
            "/api/v1/attendance/store",
            headers={"Authorization": f"Bearer {token1}"},
            json={"store_id": str(store_id)},
        )
        assert a1.status_code == 200, a1.text

        # /me 로 device_name 확인
        me1 = await async_client.get(
            "/api/v1/attendance/me",
            headers={"Authorization": f"Bearer {token1}"},
        )
        assert me1.status_code == 200
        assert me1.json()["device_name"] == "NB001", me1.json()

        # 두 번째 기기 등록 + assign
        r2 = await async_client.post(
            "/api/v1/attendance/register",
            json={"access_code": attendance_access_code},
        )
        assert r2.status_code == 201
        token2 = r2.json()["token"]
        _session_created_device_ids.append(_UUID(r2.json()["device_id"]))
        a2 = await async_client.put(
            "/api/v1/attendance/store",
            headers={"Authorization": f"Bearer {token2}"},
            json={"store_id": str(store_id)},
        )
        assert a2.status_code == 200, a2.text
        me2 = await async_client.get(
            "/api/v1/attendance/me",
            headers={"Authorization": f"Bearer {token2}"},
        )
        assert me2.status_code == 200
        assert me2.json()["device_name"] == "NB002", me2.json()
    finally:
        async with async_session() as sess:
            await sess.execute(
                _delete(AttendanceDevice).where(AttendanceDevice.store_id == store_id)
            )
            await sess.commit()


async def test_device_name_fallback_when_no_store_code(
    async_client: AsyncClient,
    attendance_access_code: str,
    db: AsyncSession,
    _session_created_device_ids: list,
) -> None:
    """store.code 가 null 이면 store.name 앞 두 글자 대문자 사용 (예: Hollywood → 'HO001')."""
    from uuid import UUID as _UUID

    from app.database import async_session
    from app.models.attendance_device import AttendanceDevice
    from app.models.organization import Organization, Store
    from sqlalchemy import delete as _delete

    async with async_session() as sess:
        org = (
            await sess.execute(
                select(Organization).order_by(Organization.created_at).limit(1)
            )
        ).scalar_one()
        existing = (
            await sess.execute(
                select(Store).where(
                    Store.organization_id == org.id,
                    Store.name == "Hollywood",
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            store = Store(
                organization_id=org.id,
                name="Hollywood",
                code=None,
                timezone="UTC",
                day_start_time={"all": "00:00"},
            )
            sess.add(store)
            await sess.commit()
            await sess.refresh(store)
            store_id = store.id
        else:
            existing.code = None
            existing.deleted_at = None
            existing.is_active = True
            await sess.commit()
            store_id = existing.id
        await sess.execute(
            _delete(AttendanceDevice).where(AttendanceDevice.store_id == store_id)
        )
        await sess.commit()

    try:
        r = await async_client.post(
            "/api/v1/attendance/register",
            json={"access_code": attendance_access_code},
        )
        assert r.status_code == 201
        token = r.json()["token"]
        _session_created_device_ids.append(_UUID(r.json()["device_id"]))
        a = await async_client.put(
            "/api/v1/attendance/store",
            headers={"Authorization": f"Bearer {token}"},
            json={"store_id": str(store_id)},
        )
        assert a.status_code == 200, a.text
        me = await async_client.get(
            "/api/v1/attendance/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me.status_code == 200
        # 'Hollywood' 앞 두 글자 대문자 = 'HO' → 'HO001'
        assert me.json()["device_name"] == "HO001", me.json()
    finally:
        async with async_session() as sess:
            await sess.execute(
                _delete(AttendanceDevice).where(AttendanceDevice.store_id == store_id)
            )
            await sess.commit()


async def test_delete_me_sets_revoked_at(
    async_client: AsyncClient,
    attendance_access_code: str,
    db: AsyncSession,
    _session_created_device_ids: list,
) -> None:
    resp = await async_client.post(
        "/api/v1/attendance/register",
        json={"access_code": attendance_access_code},
    )
    body = resp.json()
    token = body["token"]
    device_id = UUID(body["device_id"])
    _session_created_device_ids.append(device_id)

    resp_del = await async_client.delete(
        "/api/v1/attendance/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp_del.status_code == 204

    # verify DB
    row = (
        await db.execute(select(AttendanceDevice).where(AttendanceDevice.id == device_id))
    ).scalar_one()
    assert row.revoked_at is not None

    # subsequent authed call fails
    resp_me = await async_client.get(
        "/api/v1/attendance/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp_me.status_code == 401
