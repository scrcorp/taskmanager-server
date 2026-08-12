"""구버전 어댑터(F-adapter) + 키오스크 날짜 조회(F5) + 휴게 계약(F6).

여기서 고정하는 것:

1. **구버전 HTMA 만 force 취급** — `X-App-Version` 이 없거나 임계 버전 미만이면
   서버가 예전처럼 force 를 박는다. 임계 버전 이상이면 클라 값을 존중해 409.
   구버전엔 409 확인 모달이 없어 겹침이 "원인 모를 저장 실패"로 보이기 때문이다.
2. **roster 를 영업일로 조회** — D10-1 로 날짜 제약이 풀렸는데 목록이 오늘만
   반환하면 계약만 열리고 기능이 없다(F5). 미지정이면 오늘(현행 동작 보존).
3. **휴게 왕복** — 응답에 휴게 시각이 실리고, 요청으로 설정/이동/삭제가 된다(F6).
   지우는 방법은 **null 하나**다(B7) — 부분 전송 의미론을 만들지 않는다.
4. **앱이 기본 근무 길이 설정을 읽는 통로** — `GET /attendance/me` piggyback(D8-2).

주의 — 이 파일의 테스트는 **매장 배정과 매장 설정을 바꾼다.** 반드시 원복한다
(설정이 남으면 뒤따르는 테스트의 no_show/late/IDOR 판정이 통째로 바뀐다).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.core.app_version_compat import APP_VERSION_HEADER
from app.database import async_session
from app.models.schedule import Schedule
from app.models.settings import StoreSetting
from app.models.user_store import UserStore


pytestmark = pytest.mark.asyncio

MANAGE_URL = "/api/v1/attendance/manage/schedules"
DEFAULT_SHIFT_KEY = "work.default_schedule_duration_minutes"

# 이 버전부터 클라이언트가 force 를 직접 보낸다 (app_version_compat 참조).
NEW_VERSION = "1.0.17+38"
OLD_VERSION = "1.0.16+37"


# ── 헬퍼 ───────────────────────────────────────────────────────────


async def _ensure_user_store(user_id: UUID, store_id: UUID, *, is_manager: bool) -> None:
    async with async_session() as db:
        existing = (await db.execute(
            select(UserStore).where(
                UserStore.user_id == user_id, UserStore.store_id == store_id,
            )
        )).scalar_one_or_none()
        if existing is None:
            db.add(UserStore(
                user_id=user_id, store_id=store_id,
                is_manager=is_manager, is_work_assignment=True,
            ))
        elif is_manager and not existing.is_manager:
            existing.is_manager = True
        await db.commit()


@pytest_asyncio.fixture
async def gm_headers(
    async_client: AsyncClient, device_auth_headers: dict, test_users: dict,
    test_store_id: UUID,
) -> dict:
    """GM manage 세션 헤더 (버전 헤더 없음 = 구버전 클라이언트)."""
    await _ensure_user_store(test_users["testgm"]["id"], test_store_id, is_manager=True)
    resp = await async_client.post(
        "/api/v1/attendance/manage/session",
        headers=device_auth_headers,
        json={"pin": test_users["testgm"]["clockin_pin"]},
    )
    assert resp.status_code == 201, resp.text
    return {**device_auth_headers, "X-Manage-Session": resp.json()["manage_token"]}


@pytest_asyncio.fixture
async def staff_in_store(test_user: dict, test_store_id: UUID) -> None:
    await _ensure_user_store(test_user["id"], test_store_id, is_manager=False)


async def _create(async_client: AsyncClient, headers: dict, user_id: UUID, **body):
    return await async_client.post(
        MANAGE_URL, headers=headers, json={"user_id": str(user_id), **body},
    )


# ── 구버전 force 어댑터 ────────────────────────────────────────────


@pytest.mark.parametrize(
    "version_header",
    [None, OLD_VERSION],
    ids=["no-header", "below-threshold"],
)
async def test_legacy_client_overlap_saves_without_confirmation(
    async_client: AsyncClient, gm_headers: dict, test_user: dict,
    staff_in_store: None, version_header: str | None,
) -> None:
    """구버전은 겹침을 확인 없이 저장한다 — 예전 동작 유지(리스크 Q1 수용).

    헤더가 아예 없는 경우가 **현재 HTMA 전부**다. 앱은 응답 헤더(X-App-*)만 읽고
    요청 헤더는 보내지 않는다. 그래서 "헤더 없음 = 구버전"이 기본이어야 한다.
    """
    headers = dict(gm_headers)
    if version_header is not None:
        headers[APP_VERSION_HEADER] = version_header

    target = datetime.now(timezone.utc).date() - timedelta(days=21)
    first = await _create(
        async_client, headers, test_user["id"],
        start_time="10:00", end_time="14:00", operating_day=target.isoformat(),
    )
    assert first.status_code == 201, first.text

    overlap = await _create(
        async_client, headers, test_user["id"],
        start_time="12:00", end_time="16:00", operating_day=target.isoformat(),
    )
    assert overlap.status_code == 201, overlap.text


async def test_new_client_overlap_requires_confirmation(
    async_client: AsyncClient, gm_headers: dict, test_user: dict,
    staff_in_store: None,
) -> None:
    """임계 버전 이상이면 클라가 보낸 force(=기본 False)를 존중해 409."""
    headers = {**gm_headers, APP_VERSION_HEADER: NEW_VERSION}
    target = datetime.now(timezone.utc).date() - timedelta(days=22)
    first = await _create(
        async_client, headers, test_user["id"],
        start_time="10:00", end_time="14:00", operating_day=target.isoformat(),
    )
    assert first.status_code == 201, first.text

    overlap = await _create(
        async_client, headers, test_user["id"],
        start_time="12:00", end_time="16:00", operating_day=target.isoformat(),
    )
    assert overlap.status_code == 409, overlap.text
    assert overlap.json()["detail"]["retry"] == {"force": True}


async def test_unparsable_version_header_is_treated_as_legacy(
    async_client: AsyncClient, gm_headers: dict, test_user: dict,
    staff_in_store: None,
) -> None:
    """해석 못 하는 버전 문자열은 구버전 취급 — 모르는 클라를 409 로 막지 않는다."""
    headers = {**gm_headers, APP_VERSION_HEADER: "dev-build"}
    target = datetime.now(timezone.utc).date() - timedelta(days=23)
    assert (await _create(
        async_client, headers, test_user["id"],
        start_time="10:00", end_time="14:00", operating_day=target.isoformat(),
    )).status_code == 201
    assert (await _create(
        async_client, headers, test_user["id"],
        start_time="12:00", end_time="16:00", operating_day=target.isoformat(),
    )).status_code == 201


# ── F5. 영업일 지정 조회 ───────────────────────────────────────────


async def test_roster_lists_requested_operating_day(
    async_client: AsyncClient, gm_headers: dict, test_user: dict,
    staff_in_store: None,
) -> None:
    """`operating_day` 로 다른 날 스케줄을 조회한다. 미지정이면 오늘(현행 동작)."""
    target = datetime.now(timezone.utc).date() - timedelta(days=24)
    created = await _create(
        async_client, gm_headers, test_user["id"],
        start_time="10:00", end_time="14:00", operating_day=target.isoformat(),
    )
    assert created.status_code == 201, created.text
    sid = created.json()["schedule_id"]

    picked = await async_client.get(
        MANAGE_URL, headers=gm_headers, params={"operating_day": target.isoformat()},
    )
    assert picked.status_code == 200, picked.text
    rows = picked.json()
    assert sid in [r["schedule_id"] for r in rows]
    # 어느 영업일의 결과인지 행마다 알 수 있어야 한다.
    assert {r["operating_day"] for r in rows} == {target.isoformat()}

    # 기본값은 오늘 — 과거 스케줄이 섞여 나오면 기존 화면이 오염된다.
    today_only = await async_client.get(MANAGE_URL, headers=gm_headers)
    assert today_only.status_code == 200, today_only.text
    assert sid not in [r["schedule_id"] for r in today_only.json()]


async def test_roster_rejects_malformed_operating_day(
    async_client: AsyncClient, gm_headers: dict,
) -> None:
    """날짜가 아닌 값은 422 — 조용히 오늘로 떨어지면 매니저는 빈 화면의 이유를 모른다."""
    resp = await async_client.get(
        MANAGE_URL, headers=gm_headers, params={"operating_day": "yesterday"},
    )
    assert resp.status_code == 422, resp.text


# ── F6. 휴게 왕복 ──────────────────────────────────────────────────


async def test_break_round_trip_set_move_and_clear(
    async_client: AsyncClient, gm_headers: dict, test_user: dict,
    staff_in_store: None,
) -> None:
    """휴게를 만들고 → 시프트와 함께 옮기고 → null 로 지운다(B2·B4·B7)."""
    target = datetime.now(timezone.utc).date() - timedelta(days=25)
    created = await _create(
        async_client, gm_headers, test_user["id"],
        start_time="10:00", end_time="18:00", operating_day=target.isoformat(),
        break_start_time="13:00", break_end_time="13:30",
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["break_start_time"] == "13:00"
    assert body["break_end_time"] == "13:30"
    assert body["break_start_at"] == f"{target.isoformat()}T13:00"
    sid = body["schedule_id"]

    # 목록에도 실린다 — 앱이 값을 못 읽으면 동반 이동/삭제를 만들 수 없다.
    listed = await async_client.get(
        MANAGE_URL, headers=gm_headers, params={"operating_day": target.isoformat()},
    )
    row = next(r for r in listed.json() if r["schedule_id"] == sid)
    assert row["break_start_time"] == "13:00"

    # 시프트를 2시간 뒤로 옮기며 휴게도 같이 보낸다(클라가 계산해 전체 전송).
    moved = await async_client.patch(
        f"{MANAGE_URL}/{sid}", headers=gm_headers,
        json={
            "start_time": "12:00", "end_time": "20:00",
            "break_start_time": "15:00", "break_end_time": "15:30",
        },
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["break_start_time"] == "15:00"

    # null 둘 = 지움. 지운 뒤에도 근무 시각은 그대로.
    cleared = await async_client.patch(
        f"{MANAGE_URL}/{sid}", headers=gm_headers,
        json={
            "start_time": "12:00", "end_time": "20:00",
            "break_start_time": None, "break_end_time": None,
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["break_start_time"] is None
    assert cleared.json()["break_end_time"] is None
    assert cleared.json()["start_time"] == "12:00"

    async with async_session() as db:
        sched = await db.scalar(select(Schedule).where(Schedule.id == UUID(sid)))
        assert sched is not None
        assert sched.break_start_at is None and sched.break_end_at is None


async def test_break_untouched_when_field_omitted(
    async_client: AsyncClient, gm_headers: dict, test_user: dict,
    staff_in_store: None,
) -> None:
    """휴게 키를 아예 안 보내면 기존값 유지 — 휴게를 모르는 구버전이 날리면 안 된다."""
    target = datetime.now(timezone.utc).date() - timedelta(days=26)
    created = await _create(
        async_client, gm_headers, test_user["id"],
        start_time="10:00", end_time="18:00", operating_day=target.isoformat(),
        break_start_time="13:00", break_end_time="13:30",
    )
    assert created.status_code == 201, created.text
    sid = created.json()["schedule_id"]

    resp = await async_client.patch(
        f"{MANAGE_URL}/{sid}", headers=gm_headers,
        json={"start_time": "10:00", "end_time": "19:00"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["break_start_time"] == "13:00"


async def test_overnight_break_anchors_to_shift_not_operating_day(
    async_client: AsyncClient, gm_headers: dict, test_user: dict,
    staff_in_store: None,
) -> None:
    """자정 넘긴 근무의 휴게는 +1일에 앵커된다.

    영업일에 앵커하면 21:00~02:00 근무의 01:00 휴게가 근무창 **앞**으로 나가
    BREAK_OUTSIDE_SHIFT 로 저장이 거부된다 — 실제로 막히던 경로다.
    """
    target = datetime.now(timezone.utc).date() - timedelta(days=27)
    created = await _create(
        async_client, gm_headers, test_user["id"],
        start_time="21:00", end_time="02:00", operating_day=target.isoformat(),
        break_start_time="01:00", break_end_time="01:30",
    )
    assert created.status_code == 201, created.text
    next_day = (target + timedelta(days=1)).isoformat()
    assert created.json()["break_start_at"] == f"{next_day}T01:00"
    assert created.json()["end_at"] == f"{next_day}T02:00"


async def test_break_pair_must_be_complete(
    async_client: AsyncClient, gm_headers: dict, test_user: dict,
    staff_in_store: None,
) -> None:
    """한쪽만 보내면 거부 — 반쪽 휴게는 net 계산을 오염시킨다."""
    target = datetime.now(timezone.utc).date() - timedelta(days=28)
    resp = await _create(
        async_client, gm_headers, test_user["id"],
        start_time="10:00", end_time="18:00", operating_day=target.isoformat(),
        break_start_time="13:00",
    )
    assert resp.status_code == 400, resp.text


# ── D8-2. 기본 근무 길이 설정 통로 ────────────────────────────────


@pytest_asyncio.fixture
async def default_shift_90(test_store_id: UUID) -> AsyncIterator[None]:
    """매장 수준으로 기본 근무 길이를 90분으로. 끝나면 **행을 지워** 원복."""
    from app.main import seed_settings_registry

    await seed_settings_registry()
    async with async_session() as db:
        db.add(StoreSetting(store_id=test_store_id, key=DEFAULT_SHIFT_KEY, value=90))
        await db.commit()
    yield
    async with async_session() as db:
        await db.execute(delete(StoreSetting).where(
            StoreSetting.store_id == test_store_id, StoreSetting.key == DEFAULT_SHIFT_KEY,
        ))
        await db.commit()


async def test_device_me_exposes_default_shift_minutes(
    async_client: AsyncClient, device_auth_headers: dict,
) -> None:
    """설정 override 가 없으면 registry 기본값(330)이 그대로 실린다."""
    resp = await async_client.get("/api/v1/attendance/me", headers=device_auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["default_schedule_duration_minutes"] == 330


async def test_device_me_follows_store_setting(
    async_client: AsyncClient, device_auth_headers: dict, default_shift_90: None,
) -> None:
    """매장이 값을 바꾸면 기기가 그 값을 본다 — 앱 하드코딩 330 을 없애는 통로(D8-2)."""
    resp = await async_client.get("/api/v1/attendance/me", headers=device_auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["default_schedule_duration_minutes"] == 90
