"""Integration — 영업일 창 밖 시프트는 clock-in 후보가 아니다 + 경계값 페이로드.

**사고 재현**: 경계 11:00 매장에서 `operating_day=8/18` 인데 `start_at=8/19 17:00`
(= 8/19 영업일의 창) 인 스케줄이 24건 저장됐다. clock-in 후보 조회가
`operating_day == today` 만 봤기 때문에 8/18 에 출근하면 그 시프트가 잡혔고,
"1439분 조기출근" 으로 기록되어 급여 확정 게이트에 걸렸다.

저장 단계 검증(START_DATE_MISMATCH)이 지금은 막지만, **이미 저장된 행**과 SQL 직접
수정·임포트 경로는 그 검증을 지나가지 않는다. 아래가 UI 와 무관한 마지막 방어선이다.
목록(identify)과 clock-in 이 **같은 규칙**을 쓰는지도 함께 본다 — 갈리면
"목록엔 보이는데 찍으면 거부" 가 된다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.database import async_session
from app.models.user_store import UserStore

pytestmark = [
    pytest.mark.asyncio,
    # "지금 기준 ±N시간" 시프트를 만들므로 경계가 now 근처면 시나리오가 성립하지 않는다.
    pytest.mark.usefixtures("centered_day_boundary"),
]

CLOCK_IN_URL = "/api/v1/attendance/clock-in"
IDENTIFY_URL = "/api/v1/attendance/identify-by-pin"
ME_URL = "/api/v1/attendance/me"


@pytest_asyncio.fixture
async def staff_in_store(test_user: dict, test_store_id: UUID) -> None:
    async with async_session() as db:
        existing = await db.scalar(
            select(UserStore).where(
                UserStore.user_id == test_user["id"],
                UserStore.store_id == test_store_id,
            )
        )
        if existing is None:
            db.add(UserStore(user_id=test_user["id"], store_id=test_store_id))
            await db.commit()


async def _day_config(store_id: UUID):
    from app.utils.timezone import get_store_day_config, get_work_date, operating_day_window

    async with async_session() as db:
        tz_name, day_cfg = await get_store_day_config(db, store_id)
    today = get_work_date(tz_name, day_cfg, datetime.now(timezone.utc))
    start, end = operating_day_window(tz_name, day_cfg, today)
    # 스케줄 start_at/end_at 은 naive 벽시계다.
    return tz_name, today, start.replace(tzinfo=None), end.replace(tzinfo=None)


async def _corrupted_schedule(
    make_schedule, test_user: dict, store_id: UUID, *, after: bool = False
) -> UUID:
    """`operating_day=오늘` 라벨을 달고 **다른 영업일의 창**에서 시작하는 시프트.

    기본은 **창 앞**(= 어제 영업일 시각)이다. 시간순으로 오늘 시프트보다 앞서기 때문에
    "시간순 첫 미출근" 규칙이 이걸 먼저 고르며, 그게 사고 때 벌어진 일이다.
    `after=True` 는 창 뒤(= 내일 영업일 시각) — 사고 데이터와 같은 방향이다.

    `work_date` 를 명시해 팩토리의 영업일 파생을 우회한다 — 정상 경로로는 만들 수 없는
    행이라, 우회해서 만드는 것 자체가 이 테스트의 요점이다.
    """
    _tz, today, window_start, window_end = await _day_config(store_id)
    start_at = window_end + timedelta(hours=1) if after else window_start - timedelta(hours=2)
    return await make_schedule(
        test_user,
        work_date=today,
        start_at=start_at,
        end_at=start_at + timedelta(hours=4),
    )


async def _healthy_schedule(make_schedule, test_user: dict, store_id: UUID) -> UUID:
    """지금 시작하는 정상 시프트 (창 안)."""
    tz_name, _today, _ws, _we = await _day_config(store_id)
    local_now = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).replace(
        second=0, microsecond=0, tzinfo=None
    )
    return await make_schedule(
        test_user, start_at=local_now, end_at=local_now + timedelta(hours=4)
    )


async def _ensure_attendance(schedule_id: UUID) -> None:
    from app.models.schedule import Schedule
    from app.services.attendance_lifecycle_service import (
        ensure_attendance_for_schedule,
    )

    async with async_session() as db:
        sched = await db.scalar(select(Schedule).where(Schedule.id == schedule_id))
        await ensure_attendance_for_schedule(db, sched)
        await db.commit()


async def _clock_in(client: AsyncClient, headers: dict, user: dict, **extra) -> dict:
    return await client.post(
        CLOCK_IN_URL,
        headers=headers,
        json={"user_id": str(user["id"]), "pin": user["clockin_pin"], **extra},
    )


async def test_out_of_window_shift_is_not_picked_by_clock_in(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """★ 사고 회귀 — 창 밖 시프트가 있어도 clock-in 은 정상 시프트에 붙는다.

    예전엔 창 밖 시프트가 "시간순 첫 미출근" 으로 잡혀 조기출근 사유를 요구했고,
    사유를 넣으면 1439분짜리 `early_clock_in_override` 가 기록됐다.
    """
    corrupted = await _corrupted_schedule(make_schedule, test_user, test_store_id)
    healthy = await _healthy_schedule(make_schedule, test_user, test_store_id)
    await _ensure_attendance(corrupted)
    await _ensure_attendance(healthy)

    resp = await _clock_in(async_client, device_auth_headers, test_user)

    assert resp.status_code == 200, resp.text
    assert resp.json()["schedule_id"] == str(healthy), (
        "영업일 창 밖 시프트가 clock-in 대상이 됐다 (2026-08 오염 사고 재발)"
    )


async def test_out_of_window_shift_alone_does_not_allow_clock_in(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """창 밖 시프트 하나뿐이면 출근할 대상이 없다 — 조기출근/지각으로 둔갑시키지 않는다.

    사고 데이터와 같은 방향(창 **뒤**)으로 만든다. 예전에는 이 행이 "가장 가까운 미래"
    로 잡혀 1439분짜리 조기출근 사유 요구가 나갔다.
    """
    corrupted = await _corrupted_schedule(
        make_schedule, test_user, test_store_id, after=True
    )
    await _ensure_attendance(corrupted)

    resp = await _clock_in(async_client, device_auth_headers, test_user)

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    code = detail.get("code") if isinstance(detail, dict) else None
    assert code != "early_clock_in_reason_required", (
        "창 밖 시프트를 대상으로 조기출근 사유를 요구했다"
    )


async def test_out_of_window_shift_is_listed_but_not_selectable(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """창 밖 시프트는 **목록에는 뜨되 고를 수 없다** (2026-08-19 결정).

    처음엔 목록에서 아예 뺐다. 그러면 잘못 만들어진 스케줄이 화면에서 소리 없이 사라져
    "분명 배정했는데 아무 데도 안 보인다" 가 되고, 결근인지 잘못 만든 건지도 구분할 수 없다.
    고를 수 없게 하는 것과 안 보이게 하는 것은 다르다 — 이유를 코드로 말하고 목록엔 남긴다.

    (기본 제시 shift 로도 뽑히지 않는다. 고를 수 없는 것을 기본값으로 내밀면 안 된다.)
    """
    corrupted = await _corrupted_schedule(make_schedule, test_user, test_store_id)
    healthy = await _healthy_schedule(make_schedule, test_user, test_store_id)
    await _ensure_attendance(corrupted)
    await _ensure_attendance(healthy)

    resp = await async_client.post(
        IDENTIFY_URL,
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_id = {it["schedule_id"]: it for it in body["today_attendances"]}
    assert str(corrupted) in by_id, "창 밖이라고 목록에서 지우면 아무도 그 존재를 모른다"
    assert by_id[str(corrupted)]["clock_in_eligible"] is False
    assert by_id[str(corrupted)]["ineligible_reason"] == "outside_operating_window"

    assert str(healthy) in by_id
    assert by_id[str(healthy)]["clock_in_eligible"] is True
    assert body["default_schedule_id"] == str(healthy)


async def test_clocked_in_shift_stays_listed_even_if_out_of_window(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """이미 찍은 시프트는 창 밖이어도 목록에 남는다 — 빼면 퇴근할 방법이 사라진다.

    날짜 오염의 정정은 매니저의 일이지, 근무 중인 사람의 clock-out 을 막을 이유가 아니다.
    """
    from app.models.attendance import Attendance

    corrupted = await _corrupted_schedule(make_schedule, test_user, test_store_id)
    await _ensure_attendance(corrupted)
    async with async_session() as db:
        att = await db.scalar(
            select(Attendance).where(Attendance.schedule_id == corrupted)
        )
        att.clock_in = datetime.now(timezone.utc) - timedelta(hours=1)
        att.status = "working"
        await db.commit()

    resp = await async_client.post(
        IDENTIFY_URL,
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    listed = {it["schedule_id"] for it in body["today_attendances"]}
    assert str(corrupted) in listed
    assert body["today_status"] == "working"


async def test_device_me_carries_the_store_day_boundaries(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_store_id: UUID,
    centered_day_boundary: str,
) -> None:
    """기기 페이로드에 매장 영업일 경계가 실린다 (요일 7키 전부).

    앱이 시프트의 달력 날짜를 스스로 계산하려면 이 값이 필요하다. 없으면 자정 경계를
    가정하고 그려서, 경계가 자정이 아닌 매장에서 화면 날짜와 저장 날짜가 갈린다.
    """
    resp = await async_client.get(ME_URL, headers=device_auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    boundaries = body["store_day_start_times"]
    assert set(boundaries) == {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}, (
        "요일별 구성을 표현할 수 없으면 앱이 폴백 규칙을 다시 구현해야 한다"
    )
    assert set(boundaries.values()) == {centered_day_boundary}
    # 같은 응답의 다른 설정값들과 함께 온다 — 앱이 한 번의 폴링으로 다 읽는다.
    assert body["store_timezone"] is not None
    assert body["work_date"] is not None
