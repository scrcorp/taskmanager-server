"""Integration — clock-in shift 선택(페이즈 ④) + 겹침 clock-in(페이즈 ⑤).

**이 파일의 존재 이유는 첫 테스트 하나다.**

    09-13 / 17-21 두 shift, 오전을 놓친 채 13:05 에 출근
      → 예전: 저녁(17:00) shift 가 잡혀 `early_clock_in_reason_required` 로 막히고,
              사유를 넣으면 `early_clock_in_override` + 매니저 알림 + 급여 확정 게이트.
              오전 row 는 `no_show` 로 남고 실근무가 저녁 schedule 에 붙었다.
      → 지금: 오전 shift 에 붙고 **late** 로 기록된다.

나머지 테스트는 그 수정이 열어 놓은 문을 지킨다. 겹침을 허용(D15)한 만큼
"몰라서 두 번 찍히는" 경로가 그대로 남아 있으면 급여가 두 번 나가므로,
가드가 **플래그 없이는 절대 열리지 않는다**는 것을 함께 못 박는다.

시각은 전부 "지금" 기준 상대값이다 — 고정 시각(09:00)으로 잡으면 실행 시각에 따라
조기/지각 판정이 뒤집힌다(기존 early clock-in 테스트와 같은 이유).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.database import async_session
from app.models.alert import Alert
from app.models.attendance import Attendance
from app.models.schedule import Schedule
from app.models.user_store import UserStore
from app.services.attendance_service import (
    ANOMALY_EARLY_CLOCK_IN_OVERRIDE,
    ANOMALY_OVERLAPPING_CLOCK_IN,
)

pytestmark = [
    pytest.mark.asyncio,
    # 스케줄을 "지금 기준 ±N시간" 으로 만드는 테스트들이라 매장 영업일 경계가 now 근처면
    # 시나리오가 성립하지 않는다(그 시프트가 다음 영업일 창으로 넘어간다). 경계를
    # now-5h 로 옮겨 실행 시각과 무관하게 만든다 — `centered_day_boundary` 참조.
    pytest.mark.usefixtures("centered_day_boundary"),
]

CLOCK_IN_URL = "/api/v1/attendance/clock-in"
IDENTIFY_URL = "/api/v1/attendance/identify-by-pin"


# ── 헬퍼 ────────────────────────────────────────────────────────────


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


async def _store_now(store_id: UUID) -> datetime:
    """매장 현지 벽시계 '지금'. 스케줄 start_at/end_at 은 naive 벽시계다."""
    from app.utils.timezone import get_store_day_config

    async with async_session() as db:
        tz_name, _ = await get_store_day_config(db, store_id)
    return datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).replace(
        second=0, microsecond=0, tzinfo=None
    )


async def _store_today(store_id: UUID) -> "date":
    """서버가 보는 **현재 영업일**. 달력일로 직접 계산하지 않는다.

    경계가 자정이 아니면 영업일 라벨과 UTC 캘린더 날짜가 갈린다
    (`centered_day_boundary` 가 그렇게 만든다). 라벨을 직접 계산하면 그 갈림에서
    테스트가 서버와 다른 날짜를 말하게 된다.
    """
    from app.utils.timezone import get_store_day_config, get_work_date

    async with async_session() as db:
        tz_name, day_cfg = await get_store_day_config(db, store_id)
    return get_work_date(tz_name, day_cfg, datetime.now(timezone.utc))


async def _shift_at(
    make_schedule, test_user: dict, local_now: datetime, *, starts_in: int, hours: int
) -> UUID:
    """`starts_in` 분 뒤(음수면 전)에 시작해 `hours` 시간 동안인 shift."""
    start_at = local_now + timedelta(minutes=starts_in)
    return await make_schedule(
        test_user, start_at=start_at, end_at=start_at + timedelta(hours=hours)
    )


async def _ensure_attendance(schedule_id: UUID) -> None:
    """스케줄에 딸린 attendance row 를 만든다(운영에서는 eager 훅이 한다)."""
    from app.services.attendance_lifecycle_service import (
        ensure_attendance_for_schedule,
    )

    async with async_session() as db:
        sched = await db.scalar(select(Schedule).where(Schedule.id == schedule_id))
        await ensure_attendance_for_schedule(db, sched)
        await db.commit()


async def _attendance_for(schedule_id: UUID) -> Attendance | None:
    async with async_session() as db:
        return await db.scalar(
            select(Attendance).where(Attendance.schedule_id == schedule_id)
        )


async def _clock_in(client: AsyncClient, headers: dict, user: dict, **extra) -> dict:
    return await client.post(
        CLOCK_IN_URL,
        headers=headers,
        json={
            "user_id": str(user["id"]),
            "pin": user["clockin_pin"],
            **extra,
        },
    )


# ---------------------------------------------------------------------------
# ④ shift 선택 — 원인 A 회귀
# ---------------------------------------------------------------------------


async def test_missed_morning_shift_wins_over_evening_shift(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """★ 이 트랙의 핵심 회귀 테스트.

    오전 shift 를 놓친 채 그 종료 직후에 출근하면 **오전 shift 에 붙고 late** 다.
    예전 fallback 우선순위("가장 가까운 미래")는 저녁 shift 를 골랐고, 그래서
    지각이 조기출근 사유 요구로 이어졌다.
    """
    local_now = await _store_now(test_store_id)
    # 오전: 4시간 전 시작 → 5분 전 종료 (미출근인 채로 지나갔다)
    morning = await _shift_at(
        make_schedule, test_user, local_now, starts_in=-245, hours=4
    )
    # 저녁: 4시간 뒤 시작
    evening = await _shift_at(
        make_schedule, test_user, local_now, starts_in=240, hours=4
    )
    await _ensure_attendance(morning)
    await _ensure_attendance(evening)

    resp = await _clock_in(async_client, device_auth_headers, test_user)

    # 조기 출근 사유를 요구받지 않는다 — 애초에 조기가 아니다.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schedule_id"] == str(morning), (
        "놓친 오전 shift 가 아니라 다른 shift 에 붙었다 (원인 A 재발)"
    )
    assert body["status"] == "late"
    assert "late" in (body.get("anomalies") or [])
    assert ANOMALY_EARLY_CLOCK_IN_OVERRIDE not in (body.get("anomalies") or [])

    # 저녁 shift 는 손대지 않는다.
    evening_att = await _attendance_for(evening)
    assert evening_att is not None and evening_att.clock_in is None


async def test_identify_previews_each_candidate_and_marks_the_default(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """후보마다 "지금 고르면 어떻게 기록되는가" 가 숫자로 실린다 (D3).

    앱은 임계값을 모르고 어떤 판정도 하지 않는다 — 숫자를 받아 l10n 문구로 조립만
    한다. 프리뷰가 없으면 "시간순 첫 미출근" 기본값이 오선택을 유도할 때 직원이
    알아챌 방법이 없다(그게 유일한 방어선이다).
    """
    local_now = await _store_now(test_store_id)
    morning = await _shift_at(
        make_schedule, test_user, local_now, starts_in=-245, hours=4
    )
    evening = await _shift_at(
        make_schedule, test_user, local_now, starts_in=240, hours=4
    )
    await _ensure_attendance(morning)
    await _ensure_attendance(evening)

    resp = await async_client.post(
        IDENTIFY_URL,
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["default_schedule_id"] == str(morning)
    assert body["server_time"] is not None
    items = {it["schedule_id"]: it for it in body["today_attendances"]}
    assert set(items) == {str(morning), str(evening)}

    # 정렬 불변식 — 0번은 언제나 "그대로 써도 되는 대상". 구버전 HTMA 는 이 값을
    # 무조건 자동 선택하므로, 이 한 줄이 구버전까지 원인 A 를 교정한다.
    assert body["today_attendances"][0]["schedule_id"] == str(morning)

    morning_item = items[str(morning)]
    assert morning_item["is_default"] is True
    assert morning_item["clock_in_eligible"] is True
    assert morning_item["ineligible_reason"] is None
    preview = morning_item["clock_in_preview"]
    assert preview["kind"] == "late"
    assert preview["reason_required"] is False
    assert preview["minutes_early"] == 0
    # 09:00 시작 → 13:05 = 245분. 실행 지연으로 몇 초 밀릴 수 있어 폭을 준다.
    assert 243 <= preview["minutes_late"] <= 246

    evening_item = items[str(evening)]
    assert evening_item["is_default"] is False
    assert evening_item["clock_in_eligible"] is True
    evening_preview = evening_item["clock_in_preview"]
    assert evening_preview["kind"] == "early"
    # 조기 출근이라 사유 시트가 뜰 것을 앱이 **미리** 알 수 있어야 한다(400 왕복 예고).
    assert evening_preview["reason_required"] is True
    assert 238 <= evening_preview["minutes_early"] <= 241
    assert evening_preview["minutes_late"] == 0


async def test_finished_shift_is_listed_but_not_selectable(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """끝난 shift 는 목록에 남되 `clock_in_eligible=false` + 프리뷰 없음 (D14).

    목록에서 지워버리면 직원이 "내 오전 근무가 어디 갔지" 를 확인할 방법이 없고,
    프리뷰를 주면 "고를 수 있나?" 로 읽힌다.
    """
    local_now = await _store_now(test_store_id)
    done = await _shift_at(make_schedule, test_user, local_now, starts_in=-245, hours=4)
    live = await _shift_at(make_schedule, test_user, local_now, starts_in=-30, hours=4)
    await _ensure_attendance(done)
    await _ensure_attendance(live)
    async with async_session() as db:
        att = await db.scalar(select(Attendance).where(Attendance.schedule_id == done))
        att.clock_in = datetime.now(timezone.utc) - timedelta(hours=4)
        att.clock_out = datetime.now(timezone.utc) - timedelta(minutes=5)
        att.status = "clocked_out"
        await db.commit()

    resp = await async_client.post(
        IDENTIFY_URL,
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = {it["schedule_id"]: it for it in body["today_attendances"]}

    done_item = items[str(done)]
    assert done_item["clock_in_eligible"] is False
    assert done_item["ineligible_reason"] == "already_completed"
    assert done_item["clock_in_preview"] is None
    # 끝난 shift 는 절대 0번(=구버전이 자동 선택하는 자리)에 오지 않는다.
    assert body["today_attendances"][0]["schedule_id"] == str(live)
    assert body["default_schedule_id"] == str(live)


# ---------------------------------------------------------------------------
# ④ D4 — 어제 영업일(야간조) 후보
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def last_night_shift(
    make_schedule, test_user: dict, test_store_id: UUID
) -> UUID:
    """어제 영업일 라벨의 야간 shift — 2시간 전에 끝났고 아직 안 찍었다.

    매장 day_start 경계를 넘겨 지각한 야간조가 정확히 이 모습이다. 후보 조회가
    `operating_day == today` 뿐이던 시절엔 이 shift 가 아예 보이지 않아 오늘의
    다른 shift(또는 워크인)에 근무가 붙었다.
    """
    local_now = await _store_now(test_store_id)
    yesterday = await _store_today(test_store_id) - timedelta(days=1)
    schedule_id = await make_schedule(
        test_user,
        work_date=yesterday,
        start_at=local_now - timedelta(hours=6),
        end_at=local_now - timedelta(hours=2),
    )
    await _ensure_attendance(schedule_id)
    return schedule_id


async def test_last_nights_shift_is_offered_but_never_auto_selected(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
    last_night_shift: UUID,
) -> None:
    """어제 후보는 목록에 **보이되**, 서버 fallback 은 절대 고르지 않는다.

    명시 선택은 사람의 판단이지만 fallback 은 추측이고, 추측이 영업일을 하루
    건너뛰면 급여 귀속 기간이 통째로 밀린다. 그래서 자동 선택은 오늘 후보만 본다.
    """
    identified = await async_client.post(
        IDENTIFY_URL,
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"]},
    )
    assert identified.status_code == 200, identified.text
    items = identified.json()["today_attendances"]
    night = next(it for it in items if it["schedule_id"] == str(last_night_shift))
    assert night["clock_in_eligible"] is True
    # 앱이 "Yesterday" 배지를 붙일 수 있는 유일한 단서.
    assert night["operating_day"] < (await _store_today(test_store_id)).isoformat()

    # 오늘 후보가 없으므로 자동 선택은 어제 shift 로 넘어가지 않고 그냥 거부한다.
    resp = await _clock_in(async_client, device_auth_headers, test_user)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "No scheduled shift for today at this store"


async def test_last_nights_shift_can_be_picked_explicitly(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
    last_night_shift: UUID,
) -> None:
    """직원이 지목하면 어제 shift 로 찍히고, **급여 귀속도 그 영업일**이다."""
    resp = await _clock_in(
        async_client, device_auth_headers, test_user,
        schedule_id=str(last_night_shift),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["schedule_id"] == str(last_night_shift)

    att = await _attendance_for(last_night_shift)
    yesterday = await _store_today(test_store_id) - timedelta(days=1)
    assert att.work_date == yesterday, "어제 shift 인데 오늘 영업일로 귀속됐다"


async def test_stale_yesterday_shift_is_dropped_after_the_grace_window(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """끝난 지 4시간이 넘은 어제 shift 는 후보에서 빠진다.

    상한이 없으면 어제 결근한 shift 가 오늘 출근을 계속 낚아챈다.
    """
    local_now = await _store_now(test_store_id)
    yesterday = await _store_today(test_store_id) - timedelta(days=1)
    stale = await make_schedule(
        test_user,
        work_date=yesterday,
        start_at=local_now - timedelta(hours=14),
        end_at=local_now - timedelta(hours=10),
    )
    await _ensure_attendance(stale)

    identified = await async_client.post(
        IDENTIFY_URL,
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"]},
    )
    assert identified.status_code == 200, identified.text
    listed = {it["schedule_id"] for it in identified.json()["today_attendances"]}
    assert str(stale) not in listed

    # 목록에 없는 것은 명시 선택으로도 못 고른다 — picker 와 서버가 갈리면 안 된다.
    resp = await _clock_in(
        async_client, device_auth_headers, test_user, schedule_id=str(stale)
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "shift_not_available"


async def test_yesterdays_missed_shift_never_takes_the_default_slot(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
    last_night_shift: UUID,
) -> None:
    """★ 어제 결근 shift 가 **오늘 shift 를 밀어내고 기본이 되면 안 된다**.

    미출근 정렬이 시간순뿐이면 어제 shift 는 항상 오늘 것보다 이르므로 무조건
    0번 + `default_schedule_id` 가 된다. 구버전 HTMA 는 `today_attendances.first`
    의 schedule_id 를 그대로 실어 보내고 서버는 그걸 '명시 선택' 으로 수용하므로,
    fallback 안전규칙(오늘 후보만 본다)이 통째로 우회되고 오늘 근무가 어제
    영업일(= 다른 급여 기간일 수 있다)에 귀속된다.
    """
    local_now = await _store_now(test_store_id)
    today_shift = await _shift_at(
        make_schedule, test_user, local_now, starts_in=30, hours=4
    )
    await _ensure_attendance(today_shift)

    identified = await async_client.post(
        IDENTIFY_URL,
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"]},
    )
    assert identified.status_code == 200, identified.text
    body = identified.json()
    listed = {it["schedule_id"] for it in body["today_attendances"]}
    # 어제 후보는 여전히 목록에 **있다** — 명시 선택은 가능해야 한다(D4).
    assert str(last_night_shift) in listed
    assert body["today_attendances"][0]["schedule_id"] == str(today_shift)
    assert body["default_schedule_id"] == str(today_shift)


async def test_active_night_shift_from_yesterday_is_offered_for_clock_out(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """★ 매장 day_start 를 넘겨 아직 진행 중인 어제 야간조가 identify 에 나온다.

    안 나오면 `today_status` 가 null 이라 앱은 Clock Out 을 아예 못 띄우고, 직원이
    Clock In 을 누르면 서버의 열린-row 가드가 'Previous shift not clocked out.'
    400 으로 막는다 — 키오스크에서 퇴근할 방법이 사라진다(매니저 모드만 가능).
    day_start 를 넘겨 끝나는 모든 야간조에 매일 재현되던 상황이다.
    """
    local_now = await _store_now(test_store_id)
    yesterday = await _store_today(test_store_id) - timedelta(days=1)
    night = await make_schedule(
        test_user,
        work_date=yesterday,
        start_at=local_now - timedelta(hours=6),
        end_at=local_now + timedelta(hours=1),  # 아직 안 끝났다
    )
    await _ensure_attendance(night)
    async with async_session() as db:
        att = await db.scalar(select(Attendance).where(Attendance.schedule_id == night))
        att.clock_in = datetime.now(timezone.utc) - timedelta(hours=6)
        att.status = "working"
        await db.commit()

    identified = await async_client.post(
        IDENTIFY_URL,
        headers=device_auth_headers,
        json={"pin": test_user["clockin_pin"]},
    )
    assert identified.status_code == 200, identified.text
    body = identified.json()
    assert body["today_status"] == "working"
    assert body["default_schedule_id"] == str(night)
    item = body["today_attendances"][0]
    assert item["schedule_id"] == str(night)
    # clock-in 대상은 아니다 — 이미 찍었다. "화면의 주인공" 과 "고를 수 있나" 는 별개.
    assert item["clock_in_eligible"] is False
    assert item["ineligible_reason"] == "already_clocked_in"
    # 목록이 바로 Clock Out 을 제시하는데 "지난 근무가 안 닫혔다" 경고까지 띄우면
    # 해결책 옆에서 사고처럼 보인다 — 목록에 실린 건은 stale 에서 뺀다.
    assert body["stale_attendances"] == []


async def test_active_night_shift_from_yesterday_can_actually_clock_out(
    async_client: AsyncClient,
    device_auth_headers: dict,
    make_schedule,
    test_user: dict,
    test_store_id: UUID,
    staff_in_store: None,
) -> None:
    """★ 목록에 뜨는 것과 **실제로 퇴근이 되는 것**은 다르다 — 끝까지 확인한다.

    바로 위 테스트는 identify 가 어제 영업일의 진행 중 shift 를 보여주는 것까지만 본다.
    그런데 앱이 퇴근을 누르면 서버는 다시 "오늘" 기준으로 대상 row 를 찾는다. 그 조회가
    오늘 라벨만 본다면 화면엔 Clock Out 이 떠 있는데 누르면 실패하는, 가장 나쁜 형태가 된다.
    (서버는 `perform_clock_action` 의 `open_prev` 로 전날 라벨의 열린 row 를 함께 본다.)
    """
    local_now = await _store_now(test_store_id)
    yesterday = await _store_today(test_store_id) - timedelta(days=1)
    night = await make_schedule(
        test_user,
        work_date=yesterday,
        start_at=local_now - timedelta(hours=6),
        end_at=local_now + timedelta(hours=1),
    )
    await _ensure_attendance(night)
    async with async_session() as db:
        att = await db.scalar(select(Attendance).where(Attendance.schedule_id == night))
        att.clock_in = datetime.now(timezone.utc) - timedelta(hours=6)
        att.status = "working"
        await db.commit()

    # 예정 종료보다 1시간 이르므로 조기 퇴근 사유가 필요하다 — 그 400 이 뜬다는 것 자체가
    # 서버가 **어제 라벨의 그 row 를 대상으로 잡았다**는 증거다(대상을 못 찾으면 다른 에러다).
    base = {"user_id": str(test_user["id"]), "pin": test_user["clockin_pin"]}
    co = await async_client.post(
        "/api/v1/attendance/clock-out",
        headers=device_auth_headers,
        json={**base, "reason": "closing early"},
    )
    assert co.status_code == 200, co.text

    async with async_session() as db:
        closed = await db.scalar(select(Attendance).where(Attendance.schedule_id == night))
        assert closed.clock_out is not None, "어제 라벨 근무가 닫히지 않았다"
        assert closed.status == "clocked_out"


# ---------------------------------------------------------------------------
# ⑤ 겹침 clock-in — 가드는 플래그 + 명시 선택일 때만 열린다
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def two_open_shifts(
    make_schedule, test_user: dict, test_store_id: UUID
) -> tuple[UUID, UUID]:
    """시간이 겹치는 두 미출근 shift (둘 다 이미 시작했다)."""
    local_now = await _store_now(test_store_id)
    first = await _shift_at(make_schedule, test_user, local_now, starts_in=-180, hours=4)
    second = await _shift_at(make_schedule, test_user, local_now, starts_in=-60, hours=4)
    await _ensure_attendance(first)
    await _ensure_attendance(second)
    return first, second


async def test_second_clock_in_without_schedule_id_is_still_blocked(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    staff_in_store: None,
    two_open_shifts: tuple[UUID, UUID],
) -> None:
    """★ 구버전 안전장치 — 자동 선택으로는 겹침이 **절대** 열리지 않는다.

    키오스크 더블탭과 네트워크 재시도가 조용히 두 row 를 만드는 것을 지금 이 가드
    하나가 막고 있다. 문구까지 예전 그대로여야 구버전 HTMA 가 같은 화면을 보여준다.
    """
    first, _second = two_open_shifts
    ok = await _clock_in(async_client, device_auth_headers, test_user)
    assert ok.status_code == 200, ok.text
    assert ok.json()["schedule_id"] == str(first)

    again = await _clock_in(async_client, device_auth_headers, test_user)
    assert again.status_code == 400, again.text
    assert again.json()["detail"] == "Previous shift not clocked out. Clock out first."


async def test_same_shift_cannot_be_clocked_in_twice_even_with_allow_overlap(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    staff_in_store: None,
    two_open_shifts: tuple[UUID, UUID],
) -> None:
    """겹침 허용은 "**다른** shift" 에만 열린다 — 같은 shift 재출근은 여전히 거부."""
    first, _second = two_open_shifts
    ok = await _clock_in(async_client, device_auth_headers, test_user)
    assert ok.status_code == 200, ok.text

    again = await _clock_in(
        async_client, device_auth_headers, test_user,
        schedule_id=str(first), allow_overlap=True,
    )
    assert again.status_code == 400, again.text
    assert again.json()["detail"] == "Previous shift not clocked out. Clock out first."


async def test_overlapping_clock_in_asks_for_confirmation_first(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    staff_in_store: None,
    two_open_shifts: tuple[UUID, UUID],
) -> None:
    """다른 shift 를 지목했지만 확인 없이 보내면 400 + 구조화된 코드.

    `early_clock_in_reason_required` 와 **같은 재시도 형태**다 — 앱이 경고를 띄우고
    같은 요청에 `allow_overlap: true` 만 붙여 다시 보낸다.
    """
    _first, second = two_open_shifts
    ok = await _clock_in(async_client, device_auth_headers, test_user)
    assert ok.status_code == 200, ok.text
    first_att_id = ok.json()["id"]

    resp = await _clock_in(
        async_client, device_auth_headers, test_user, schedule_id=str(second)
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "overlapping_clock_in_confirmation_required"
    assert detail["open_attendance_ids"] == [first_att_id]
    assert detail["open_scheduled_start_display"]
    assert detail["open_scheduled_end_display"]

    # 확인을 요구했을 뿐 아무것도 기록하지 않았다.
    second_att = await _attendance_for(second)
    assert second_att is not None and second_att.clock_in is None


async def test_confirmed_overlap_records_both_and_labels_them(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    staff_in_store: None,
    two_open_shifts: tuple[UUID, UUID],
) -> None:
    """확인 후에는 두 shift 가 모두 열린 채 기록되고 **양쪽 모두** 라벨이 붙는다.

    한쪽만 붙이면 매니저가 어느 상세를 먼저 열지 알 수 없어 "어느 화면에서 봐도
    드러난다" 는 목적을 잃는다. 라벨은 표시용이고, 이중 지급을 실제로 막는 것은
    급여 확정 게이트(`overlapping_attendance`)다.
    """
    first, second = two_open_shifts
    ok = await _clock_in(async_client, device_auth_headers, test_user)
    assert ok.status_code == 200, ok.text

    resp = await _clock_in(
        async_client, device_auth_headers, test_user,
        schedule_id=str(second), allow_overlap=True,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schedule_id"] == str(second)
    assert ANOMALY_OVERLAPPING_CLOCK_IN in (body.get("anomalies") or [])

    # 앱 안내 트리거 — **표시 문구는 서버가 내려보내지 않는다**(앱이 l10n 으로 조립).
    overlap = body["overlap"]
    assert overlap["is_overlapping"] is True
    assert set(overlap["other_schedule_ids"]) == {str(first)}
    assert "notice" not in overlap and "message" not in overlap

    first_att = await _attendance_for(first)
    second_att = await _attendance_for(second)
    assert ANOMALY_OVERLAPPING_CLOCK_IN in (first_att.anomalies or [])
    assert ANOMALY_OVERLAPPING_CLOCK_IN in (second_att.anomalies or [])
    assert first_att.clock_out is None and second_att.clock_out is None


async def test_confirmed_overlap_alerts_managers(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    staff_in_store: None,
    two_open_shifts: tuple[UUID, UUID],
) -> None:
    """직원은 이 상태를 스스로 정리할 수 없다(취소·정정은 매니저 권한).

    그래서 알림이 유일한 즉시 전달 경로다 — 없으면 급여 확정 시점에야 발견된다.
    """
    _first, second = two_open_shifts
    assert (await _clock_in(async_client, device_auth_headers, test_user)).status_code == 200

    resp = await _clock_in(
        async_client, device_auth_headers, test_user,
        schedule_id=str(second), allow_overlap=True,
    )
    assert resp.status_code == 200, resp.text

    async with async_session() as db:
        alerts = list(
            (
                await db.execute(
                    select(Alert).where(Alert.type == "overlapping_clock_in")
                )
            )
            .scalars()
            .all()
        )
    assert alerts, "겹침 clock-in 인데 매니저 알림이 하나도 없다"
    assert all(a.reference_type == "attendance" for a in alerts)
    assert all(a.user_id != test_user["id"] for a in alerts), "본인에게 보내면 안 된다"


async def test_overlap_label_clears_when_one_side_is_closed(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    staff_in_store: None,
    two_open_shifts: tuple[UUID, UUID],
) -> None:
    """겹침은 **파생 라벨**이다 — 겹침이 사라지면 라벨도 사라진다.

    sticky flag 로 두면 매니저가 한쪽을 정리한 뒤에도 경고가 남아, 진짜 겹침과
    구분되지 않는다. (정확성은 이 라벨이 아니라 급여 게이트가 지킨다.)
    """
    first, second = two_open_shifts
    assert (await _clock_in(async_client, device_auth_headers, test_user)).status_code == 200
    assert (
        await _clock_in(
            async_client, device_auth_headers, test_user,
            schedule_id=str(second), allow_overlap=True,
        )
    ).status_code == 200

    # 겹치는 구간이 없어지도록 첫 shift 를 두 번째 출근 **전**에 닫는다.
    async with async_session() as db:
        first_att = await db.scalar(
            select(Attendance).where(Attendance.schedule_id == first)
        )
        second_att = await db.scalar(
            select(Attendance).where(Attendance.schedule_id == second)
        )
        first_att.clock_out = second_att.clock_in - timedelta(minutes=10)
        first_att.status = "clocked_out"
        await db.commit()

    # clock-out 은 겹침을 없앨 수 있는 지점이므로 라벨을 다시 계산한다.
    resp = await async_client.post(
        "/api/v1/attendance/clock-out",
        headers=device_auth_headers,
        json={
            "user_id": str(test_user["id"]),
            "pin": test_user["clockin_pin"],
            "schedule_id": str(second),
            "reason": "shift ended",
        },
    )
    assert resp.status_code == 200, resp.text

    first_att = await _attendance_for(first)
    second_att = await _attendance_for(second)
    assert ANOMALY_OVERLAPPING_CLOCK_IN not in (first_att.anomalies or [])
    assert ANOMALY_OVERLAPPING_CLOCK_IN not in (second_att.anomalies or [])


async def test_clock_out_targets_the_shift_the_manager_named(
    async_client: AsyncClient,
    device_auth_headers: dict,
    test_user: dict,
    staff_in_store: None,
    two_open_shifts: tuple[UUID, UUID],
) -> None:
    """겹침 상태에서 `schedule_id` 를 주면 **그 shift** 가 닫힌다.

    겹침을 허용한 순간 "열린 row 중 첫 번째" 규칙만 남겨두면 어느 shift 를
    퇴근시켰는지 아무도 모르는 상태가 된다.
    """
    first, second = two_open_shifts
    assert (await _clock_in(async_client, device_auth_headers, test_user)).status_code == 200
    assert (
        await _clock_in(
            async_client, device_auth_headers, test_user,
            schedule_id=str(second), allow_overlap=True,
        )
    ).status_code == 200

    resp = await async_client.post(
        "/api/v1/attendance/clock-out",
        headers=device_auth_headers,
        json={
            "user_id": str(test_user["id"]),
            "pin": test_user["clockin_pin"],
            "schedule_id": str(second),
            "reason": "wrong shift",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["schedule_id"] == str(second)

    first_att = await _attendance_for(first)
    second_att = await _attendance_for(second)
    assert second_att.clock_out is not None
    assert first_att.clock_out is None, "지목하지 않은 shift 가 닫혔다"
