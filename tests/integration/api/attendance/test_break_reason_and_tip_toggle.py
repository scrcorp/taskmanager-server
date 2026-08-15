"""Integration — break 초과 사유 전달 경로 + tip 화면 store 토글.

**이 파일이 존재하는 이유**는 A 버그의 성격 때문이다. `validate_break_end` 는
pure unit test 32개로 완벽히 검증돼 있었지만, 라우터가 body 의 `reason` 을
서비스로 넘기지 않아 스태프가 35분 넘은 meal break 를 끝낼 방법이 없었다.
함수는 맞고 배선이 빠진 구멍이라 유닛 테스트로는 구조적으로 못 잡는다 —
그래서 여기서는 반드시 **HTTP 요청으로** 정책을 통과시킨다.

tip 토글은 같은 store 설정을 console 과 키오스크 manage 두 경로가 쓰므로,
"한쪽에서 쓴 값이 다른 쪽/다른 기기에 그대로 보이는가" 를 검증한다.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.database import async_session
from app.models.attendance import Attendance, AttendanceCorrection
from app.models.attendance_break import AttendanceBreak
from app.models.settings import StoreSetting
from app.models.user_store import UserStore

pytestmark = pytest.mark.asyncio

TIP_KEY = "attendance.tip_entry_enabled"


# ── helpers ──────────────────────────────────────────────────────────


async def _seed_registry() -> None:
    from app.main import seed_settings_registry

    await seed_settings_registry()


async def _set_store_setting(store_id: UUID, key: str, value) -> None:
    async with async_session() as db:
        existing = await db.scalar(
            select(StoreSetting).where(
                StoreSetting.store_id == store_id, StoreSetting.key == key
            )
        )
        if existing is not None:
            existing.value = value
        else:
            db.add(StoreSetting(store_id=store_id, key=key, value=value))
        await db.commit()


async def _clear_store_setting(store_id: UUID, key: str) -> None:
    async with async_session() as db:
        await db.execute(
            delete(StoreSetting).where(
                StoreSetting.store_id == store_id, StoreSetting.key == key
            )
        )
        await db.commit()


async def _ensure_user_store(user_id: UUID, store_id: UUID, *, is_manager: bool) -> None:
    async with async_session() as db:
        existing = await db.scalar(
            select(UserStore).where(
                UserStore.user_id == user_id, UserStore.store_id == store_id
            )
        )
        if existing is None:
            db.add(UserStore(user_id=user_id, store_id=store_id, is_manager=is_manager))
        elif is_manager and not existing.is_manager:
            existing.is_manager = True
        await db.commit()


async def _backdate_open_break(user_id: UUID, minutes: int) -> None:
    """열린 break 의 시작 시각을 과거로 밀어 경과 분을 강제한다."""
    async with async_session() as db:
        att_id = await db.scalar(
            select(Attendance.id)
            .where(Attendance.user_id == user_id)
            .order_by(Attendance.created_at.desc())
            .limit(1)
        )
        open_break = await db.scalar(
            select(AttendanceBreak).where(
                AttendanceBreak.attendance_id == att_id,
                AttendanceBreak.ended_at.is_(None),
            )
        )
        open_break.started_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        await db.commit()


@pytest_asyncio.fixture
async def staff_on_meal_break(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    make_schedule,
) -> dict:
    """오늘 스케줄로 출근 + unpaid_meal break 시작까지 진행한 상태.

    스케줄 시각은 **매장 현지 현재시각 기준 상대값**으로 잡는다. 고정 09:00 으로
    두면 테스트를 돌리는 시각/타임존에 따라 "Too early to clock in" 으로 깨진다.
    """
    await _ensure_user_store(test_user["id"], test_store_id, is_manager=False)
    from zoneinfo import ZoneInfo
    from app.utils.timezone import get_store_day_config

    async with async_session() as db:
        tz_name, _day_cfg = await get_store_day_config(db, test_store_id)
    local_now = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))
    # 벽시계(time)가 아니라 datetime 으로 넘긴다 — `.time()` 만 떼면 자정 직후
    # (local_now - 1h) 가 전날 시각으로 감기는데 operating_day 는 오늘이라
    # 스케줄이 ~23시간 뒤 미래로 잡히고 clock-in 이 early 로 거부된다.
    start_at_local = (local_now - timedelta(hours=1)).replace(
        second=0, microsecond=0, tzinfo=None
    )
    end_at_local = (local_now + timedelta(hours=4)).replace(
        second=0, microsecond=0, tzinfo=None
    )
    await make_schedule(test_user, start_at=start_at_local, end_at=end_at_local)
    base = {"user_id": str(test_user["id"]), "pin": test_user["clockin_pin"]}

    ci = await async_client.post(
        "/api/v1/attendance/clock-in", headers=device_auth_headers, json=base
    )
    assert ci.status_code == 200, ci.text
    bs = await async_client.post(
        "/api/v1/attendance/break-start",
        headers=device_auth_headers,
        json={**base, "break_type": "unpaid_meal"},
    )
    assert bs.status_code == 200, bs.text
    return base


# ── A. break-end reason 배선 ─────────────────────────────────────────


async def test_long_meal_break_end_rejected_without_reason(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    staff_on_meal_break: dict,
) -> None:
    """35분 초과 meal break 를 사유 없이 끝내면 거부 — 정책이 살아 있어야 한다."""
    await _backdate_open_break(test_user["id"], 40)
    resp = await async_client.post(
        "/api/v1/attendance/break-end",
        headers=device_auth_headers,
        json=staff_on_meal_break,
    )
    assert resp.status_code == 400, resp.text
    assert "reason" in resp.text.lower()


async def test_long_meal_break_end_succeeds_with_reason(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    staff_on_meal_break: dict,
) -> None:
    """A 버그 회귀 방지 — body 의 reason 이 라우터를 통과해 정책까지 도달한다.

    이 배선이 빠져 있으면 스태프는 키오스크에서 break 를 영영 끝낼 수 없다.
    """
    await _backdate_open_break(test_user["id"], 40)
    resp = await async_client.post(
        "/api/v1/attendance/break-end",
        headers=device_auth_headers,
        json={**staff_on_meal_break, "reason": "Waiting for coverage"},
    )
    assert resp.status_code == 200, resp.text

    async with async_session() as db:
        att_id = await db.scalar(
            select(Attendance.id)
            .where(Attendance.user_id == test_user["id"])
            .order_by(Attendance.created_at.desc())
            .limit(1)
        )
        att = await db.scalar(select(Attendance).where(Attendance.id == att_id))
        assert att.status == "working"
        closed = await db.scalar(
            select(AttendanceBreak).where(
                AttendanceBreak.attendance_id == att_id,
                AttendanceBreak.ended_at.is_not(None),
            )
        )
        assert closed is not None
        # 사유는 버려지지 않고 타임라인에 남아 콘솔에서 보여야 한다.
        # 무슨 액션이었나는 action, 무엇이 바뀌었나는 field_name 이 담는다
        # (break_end 액션은 status 전이 + break_end_at 전이 두 행을 남긴다).
        rows = (
            await db.execute(
                select(AttendanceCorrection).where(
                    AttendanceCorrection.attendance_id == att_id,
                    AttendanceCorrection.action == "break_end",
                )
            )
        ).scalars().all()
        assert rows, "break-end 액션이 타임라인에 남지 않았다"
        assert {r.reason for r in rows} == {"Waiting for coverage"}


async def test_meal_break_within_allowance_needs_no_reason(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    staff_on_meal_break: dict,
) -> None:
    """30~35분 구간은 사유 없이 종료 — 수정이 정상 경로를 막지 않았는지."""
    await _backdate_open_break(test_user["id"], 32)
    resp = await async_client.post(
        "/api/v1/attendance/break-end",
        headers=device_auth_headers,
        json=staff_on_meal_break,
    )
    assert resp.status_code == 200, resp.text


# ── B. tip 화면 store 토글 ───────────────────────────────────────────


@pytest_asyncio.fixture
async def _clean_tip_setting(test_store_id: UUID):
    await _seed_registry()
    yield
    await _clear_store_setting(test_store_id, TIP_KEY)


async def test_device_me_exposes_tip_entry_enabled(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_store_id: UUID,
    _clean_tip_setting: None,
) -> None:
    """DeviceMe 가 store resolve 값을 싣는다 — 기기는 이 값으로 tip 화면을 켠다."""
    await _set_store_setting(test_store_id, TIP_KEY, False)
    off = await async_client.get("/api/v1/attendance/me", headers=device_auth_headers)
    assert off.status_code == 200, off.text
    assert off.json()["tip_entry_enabled"] is False

    await _set_store_setting(test_store_id, TIP_KEY, True)
    on = await async_client.get("/api/v1/attendance/me", headers=device_auth_headers)
    assert on.json()["tip_entry_enabled"] is True


async def test_tip_entry_defaults_off_without_store_override(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_store_id: UUID,
    _clean_tip_setting: None,
) -> None:
    """store override 가 없으면 registry 기본값(false)로 내려간다."""
    await _clear_store_setting(test_store_id, TIP_KEY)
    resp = await async_client.get("/api/v1/attendance/me", headers=device_auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["tip_entry_enabled"] is False


@pytest_asyncio.fixture
async def gm_manage_headers(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_users: dict,
    test_store_id: UUID,
) -> dict:
    gm = test_users["testgm"]
    await _ensure_user_store(gm["id"], test_store_id, is_manager=True)
    resp = await async_client.post(
        "/api/v1/attendance/manage/session",
        headers=device_auth_headers,
        json={"pin": gm["clockin_pin"]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["can_manage_store_settings"] is True
    return {**device_auth_headers, "X-Manage-Session": resp.json()["manage_token"]}


@pytest_asyncio.fixture
async def sv_manage_headers(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_users: dict,
    test_store_id: UUID,
) -> dict:
    sv = test_users["testsv"]
    await _ensure_user_store(sv["id"], test_store_id, is_manager=True)
    resp = await async_client.post(
        "/api/v1/attendance/manage/session",
        headers=device_auth_headers,
        json={"pin": sv["clockin_pin"]},
    )
    assert resp.status_code == 201, resp.text
    # 진입은 되지만 설정 메뉴는 숨겨져야 한다.
    assert resp.json()["can_manage_store_settings"] is False
    return {**device_auth_headers, "X-Manage-Session": resp.json()["manage_token"]}


async def test_manage_can_read_and_write_store_settings(
    async_client: AsyncClient,
    device_auth_headers: dict,
    gm_manage_headers: dict,
    test_store_id: UUID,
    _clean_tip_setting: None,
) -> None:
    """키오스크에서 끈 값이 같은 매장의 다른 기기(DeviceMe)에도 그대로 반영된다."""
    await _set_store_setting(test_store_id, TIP_KEY, True)

    got = await async_client.get(
        "/api/v1/attendance/manage/store-settings", headers=gm_manage_headers
    )
    assert got.status_code == 200, got.text
    assert got.json()["tip_entry_enabled"] is True

    put = await async_client.put(
        "/api/v1/attendance/manage/store-settings",
        headers=gm_manage_headers,
        json={"tip_entry_enabled": False},
    )
    assert put.status_code == 200, put.text
    assert put.json()["tip_entry_enabled"] is False

    # 기기 관점 — 다음 폴링에서 받는 값
    me = await async_client.get("/api/v1/attendance/me", headers=device_auth_headers)
    assert me.json()["tip_entry_enabled"] is False

    # console 이 읽는 store 설정 row 와 동일해야 한다 (경로가 갈라지면 값이 어긋난다)
    async with async_session() as db:
        row = await db.scalar(
            select(StoreSetting).where(
                StoreSetting.store_id == test_store_id, StoreSetting.key == TIP_KEY
            )
        )
        assert row is not None and row.value is False


async def test_console_store_setting_upsert_writes_same_row(
    async_client: AsyncClient,
    admin_headers: dict,
    device_auth_headers: dict,
    test_store_id: UUID,
    _clean_tip_setting: None,
) -> None:
    """콘솔 PUT 이 실제로 값을 쓰고, 그 값을 키오스크 기기가 그대로 받는다.

    이 엔드포인트는 지금까지 테스트가 0건이었다. 라우터가 공통 서비스로 위임하도록
    바꾼 뒤 호출이 끊겨도(예: 이름 shadowing) 아무도 못 잡는 사각지대라 여기서 덮는다.
    """
    resp = await async_client.put(
        f"/api/v1/console/settings/stores/{test_store_id}",
        headers=admin_headers,
        json={"key": TIP_KEY, "value": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] is True

    async with async_session() as db:
        row = await db.scalar(
            select(StoreSetting).where(
                StoreSetting.store_id == test_store_id, StoreSetting.key == TIP_KEY
            )
        )
        assert row is not None and row.value is True

    me = await async_client.get("/api/v1/attendance/me", headers=device_auth_headers)
    assert me.json()["tip_entry_enabled"] is True


async def test_console_store_setting_rejects_unregistered_key(
    async_client: AsyncClient,
    admin_headers: dict,
    test_store_id: UUID,
) -> None:
    """공통 관문의 registry 검증이 콘솔 경로에서도 살아 있다."""
    resp = await async_client.put(
        f"/api/v1/console/settings/stores/{test_store_id}",
        headers=admin_headers,
        json={"key": "attendance.not_a_real_setting", "value": True},
    )
    assert resp.status_code == 400, resp.text


async def test_manage_store_settings_write_requires_permission(
    async_client: AsyncClient,
    sv_manage_headers: dict,
    test_store_id: UUID,
    _clean_tip_setting: None,
) -> None:
    """SV 는 manage 진입은 되지만 매장 설정은 못 바꾼다 (console 과 같은 문턱)."""
    await _set_store_setting(test_store_id, TIP_KEY, True)
    resp = await async_client.put(
        "/api/v1/attendance/manage/store-settings",
        headers=sv_manage_headers,
        json={"tip_entry_enabled": False},
    )
    assert resp.status_code == 403, resp.text

    async with async_session() as db:
        row = await db.scalar(
            select(StoreSetting).where(
                StoreSetting.store_id == test_store_id, StoreSetting.key == TIP_KEY
            )
        )
        assert row.value is True, "권한 없는 요청이 값을 바꿨다"
