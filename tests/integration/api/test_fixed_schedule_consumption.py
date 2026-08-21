"""고정 근무(Fixed Schedule) 실체화분의 **소비 경로** 통합 검증.

계약(2026-08-20 고정근무-구현계약)은 콘솔/패턴 서비스 쪽만 테스트한다. 이 파일은
크론·이벤트로 실체화된 실 행이 **staff 앱 조회**와 **HTMA(근태 기기) clock 동작**에서
일반 스케줄과 동일하게 취급되는지를 확인한다.

- staff 앱 `/app/my/schedules`: 창(2주) 안 자동 생성분이 그대로 보인다(virtual 합성 없음 = 콘솔 전용)
- HTMA `identify-by-pin` → `clock-in` → `break` → `clock-out`: 실체화 행에 attendance 가 붙는다
- HTMA `today-staff`: 실체화 행이 오늘 근무자 목록에 뜬다
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import AsyncIterator
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text

from app.database import async_session
from app.main import app
from app.models.attendance import Attendance
from app.models.availability import StaffAvailability
from app.models.schedule import Schedule
from app.models.user_store import UserStore
from app.models.work_pattern import StaffWorkPattern
from app.schemas.schedule_pattern import PatternBlockIn, PatternGroupIn
from app.services.fixed_schedule import patterns as svc
from app.utils.timezone import get_store_day_config

# 콘솔 패턴 테스트의 admin_session 헬퍼 재사용 (actor role eager 로드)
from tests.integration.api.console.test_pattern_groups import admin_session

pytestmark = pytest.mark.asyncio


async def _wipe(user_id: UUID, store_id: UUID) -> None:
    async with async_session() as db:
        await db.execute(delete(Attendance).where(
            Attendance.user_id == user_id, Attendance.store_id == store_id))
        await db.execute(delete(Schedule).where(
            Schedule.user_id == user_id, Schedule.store_id == store_id))
        await db.execute(delete(StaffWorkPattern).where(StaffWorkPattern.user_id == user_id))
        await db.execute(delete(StaffAvailability).where(StaffAvailability.user_id == user_id))
        await db.commit()


@pytest_asyncio.fixture
async def staff_at_store(async_client, test_user, test_store_id) -> AsyncIterator[dict]:
    """teststaff 를 test_store 에 work 배정. async_client(=_clean_state) 이후에 돈다."""
    async with async_session() as db:
        await db.execute(delete(UserStore).where(
            UserStore.user_id == test_user["id"], UserStore.store_id == test_store_id))
        db.add(UserStore(user_id=test_user["id"], store_id=test_store_id,
                         is_manager=False, is_work_assignment=True))
        await db.commit()
    await _wipe(test_user["id"], test_store_id)
    try:
        yield {**test_user, "store_id": test_store_id}
    finally:
        await _wipe(test_user["id"], test_store_id)
        async with async_session() as db:
            await db.execute(delete(UserStore).where(
                UserStore.user_id == test_user["id"], UserStore.store_id == test_store_id))
            await db.commit()


@pytest_asyncio.fixture
async def staff_headers() -> dict[str, str]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/app/auth/login",
            json={"username": "teststaff", "password": "1234"},
        )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _store_now(store_id: UUID) -> tuple[datetime, date]:
    """매장 tz 기준 현재 시각 + 오늘(영업일)."""
    async with async_session() as db:
        tz_name, day_start = await get_store_day_config(db, store_id)
    now = datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None, microsecond=0)
    from app.utils.timezone import get_work_date
    return now, get_work_date(tz_name, day_start)


async def _create_pattern(staff: dict, *, start: time, end: time, start_date: date):
    """모든 요일 반복 패턴 1블록 생성 → 저장 직후 창(2주) 실체화까지 수행된다."""
    async with admin_session(staff) as (db, admin):
        return await svc.create_group(
            db,
            organization_id=staff["organization_id"],
            actor=admin,
            data=PatternGroupIn(
                user_id=str(staff["id"]),
                store_id=str(staff["store_id"]),
                start_date=start_date,
                until_date=None,
                blocks=[PatternBlockIn(
                    start_time=start.strftime("%H:%M"),
                    end_time=end.strftime("%H:%M"),
                    byday=[0, 1, 2, 3, 4, 5, 6],
                )],
            ),
        )


async def _materialized(staff: dict) -> list[Schedule]:
    async with async_session() as db:
        return list((await db.execute(
            select(Schedule).where(
                Schedule.user_id == staff["id"],
                Schedule.store_id == staff["store_id"],
                Schedule.pattern_id.isnot(None),
            ).order_by(Schedule.operating_day)
        )).scalars())


# ── 1. staff 앱 조회 ────────────────────────────────────────────


async def test_staff_app_lists_materialized_pattern_rows(
    async_client: AsyncClient, staff_at_store: dict, staff_headers: dict
) -> None:
    """자동 생성분(창 2주)이 staff 앱 목록에 일반 스케줄과 동일하게 보인다."""
    _, today = await _store_now(staff_at_store["store_id"])
    await _create_pattern(staff_at_store, start=time(9, 0), end=time(17, 0), start_date=today)

    # 창 끝은 **서버 로컬 date.today() + 2주** 로 잡힌다(매장 tz 아님) → 매장 tz 가
    # 서버보다 앞선 날이면 첫 날 하나가 창 밖으로 밀려 14일이 된다.
    window_end = date.today() + timedelta(weeks=2)
    expected_days = [
        today + timedelta(days=i)
        for i in range((window_end - today).days + 1)
    ]
    rows = await _materialized(staff_at_store)
    assert [r.operating_day for r in rows] == expected_days
    assert all(r.status == "confirmed" and not r.pattern_overridden for r in rows)

    resp = await async_client.get(
        f"/api/v1/app/my/schedules?date_from={today}&date_to={expected_days[-1]}"
        "&per_page=100",
        headers=staff_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"]
    assert body["total"] == len(expected_days)
    assert len(items) == len(expected_days)
    by_day = {it["operating_day"]: it for it in items}
    assert set(by_day) == {d.isoformat() for d in expected_days}
    one = by_day[today.isoformat()]
    assert one["status"] == "confirmed"
    assert one["start_at"].startswith(f"{today}T09:00")
    assert one["end_at"].startswith(f"{today}T17:00")
    assert one["store_id"] == str(staff_at_store["store_id"])
    assert one["net_work_minutes"] == 480
    # 앱은 virtual 을 모른다 — 실 행만 온다
    assert all(not it["id"].startswith("virtual:") for it in items)


async def test_staff_app_today_mode_returns_materialized_row(
    async_client: AsyncClient, staff_at_store: dict, staff_headers: dict
) -> None:
    """파라미터 없는 today 모드(앱 홈)에서도 오늘 자동 생성분이 잡힌다."""
    _, today = await _store_now(staff_at_store["store_id"])
    await _create_pattern(staff_at_store, start=time(9, 0), end=time(17, 0), start_date=today)

    resp = await async_client.get("/api/v1/app/my/schedules", headers=staff_headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [it["operating_day"] for it in items] == [today.isoformat()]


async def test_staff_app_sees_nothing_beyond_window(
    async_client: AsyncClient, staff_at_store: dict, staff_headers: dict
) -> None:
    """창(2주) 밖은 virtual 로만 존재 → 앱에는 안 보인다(설계상 정상, 회귀 감지용)."""
    _, today = await _store_now(staff_at_store["store_id"])
    await _create_pattern(staff_at_store, start=time(9, 0), end=time(17, 0), start_date=today)

    far = today + timedelta(days=20)
    resp = await async_client.get(
        f"/api/v1/app/my/schedules?date_from={far}&date_to={far}", headers=staff_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


# ── 2. HTMA (근태 기기) ─────────────────────────────────────────


async def _pattern_covering_now(staff: dict) -> tuple[Schedule, datetime]:
    """지금 시각을 감싸는 패턴을 만들고 오늘 실체화 행을 돌려준다."""
    now, today = await _store_now(staff["store_id"])

    def floor5(dt: datetime) -> time:
        return time(dt.hour, dt.minute - dt.minute % 5)  # 패턴 시간은 5분 단위만 허용

    start_dt = now - timedelta(hours=1)
    start = time(0, 0) if start_dt.date() < now.date() else floor5(start_dt)
    end_dt = now + timedelta(hours=6)
    end = time(23, 55) if end_dt.date() > now.date() else floor5(end_dt)
    await _create_pattern(staff, start=start, end=end, start_date=today)
    rows = await _materialized(staff)
    sched = next(r for r in rows if r.operating_day == today)
    return sched, now


async def test_htma_identify_shows_materialized_shift(
    async_client: AsyncClient, staff_at_store: dict, device_auth_headers: dict
) -> None:
    """PIN 식별 응답이 자동 생성분을 오늘의 shift 로 인식한다."""
    sched, _ = await _pattern_covering_now(staff_at_store)

    resp = await async_client.post(
        "/api/v1/attendance/identify-by-pin",
        headers=device_auth_headers,
        json={"pin": staff_at_store["clockin_pin"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == str(staff_at_store["id"])
    assert body["today_status"] is not None, "실체화 행을 오늘 shift 로 못 잡음"
    assert body["scheduled_end"] is not None


async def test_htma_clock_cycle_on_materialized_shift(
    async_client: AsyncClient, staff_at_store: dict, device_auth_headers: dict
) -> None:
    """clock-in → break start/end → clock-out 전 구간이 실체화 행에 붙는다."""
    sched, _ = await _pattern_covering_now(staff_at_store)
    payload = {"user_id": str(staff_at_store["id"]), "pin": staff_at_store["clockin_pin"]}

    r_in = await async_client.post(
        "/api/v1/attendance/clock-in", headers=device_auth_headers, json=payload)
    assert r_in.status_code == 200, r_in.text
    assert r_in.json()["schedule_id"] == str(sched.id), "attendance 가 패턴 실체화 행에 연결되지 않음"

    async with async_session() as db:
        att = (await db.execute(select(Attendance).where(
            Attendance.schedule_id == sched.id))).scalar_one()
        assert att.clock_in is not None
        assert att.status in {"working", "late"}

    r_bs = await async_client.post(
        "/api/v1/attendance/break-start", headers=device_auth_headers,
        json={**payload, "break_type": "paid_10min"})
    assert r_bs.status_code == 200, r_bs.text

    # 10분 최소 휴식 정책(고정근무와 무관) 때문에 시작 시각을 11분 앞당겨 둔다
    async with async_session() as db:
        await db.execute(text(
            "UPDATE attendance_breaks SET started_at = started_at - interval '11 minutes' "
            "WHERE attendance_id = (SELECT id FROM attendances WHERE schedule_id = :sid)"
        ), {"sid": str(sched.id)})
        await db.commit()

    r_be = await async_client.post(
        "/api/v1/attendance/break-end", headers=device_auth_headers, json=payload)
    assert r_be.status_code == 200, r_be.text

    r_out = await async_client.post(
        "/api/v1/attendance/clock-out", headers=device_auth_headers,
        json={**payload, "reason": "verification test — early clock-out"})
    assert r_out.status_code == 200, r_out.text
    out = r_out.json()
    assert out["schedule_id"] == str(sched.id)
    assert out["clock_out"] is not None


async def test_htma_today_staff_includes_materialized_shift(
    async_client: AsyncClient, staff_at_store: dict, device_auth_headers: dict
) -> None:
    """오늘 근무자 목록(HTMA 첫 화면)에 자동 생성분이 뜬다."""
    sched, _ = await _pattern_covering_now(staff_at_store)

    resp = await async_client.get(
        "/api/v1/attendance/today-staff", headers=device_auth_headers)
    assert resp.status_code == 200, resp.text
    rows = [r for r in resp.json() if r["user_id"] == str(staff_at_store["id"])]
    assert rows, "실체화 행이 today-staff 에 없음"
    assert any(r.get("schedule_id") == str(sched.id) for r in rows), rows


# ── 3. 창 기준일 = org 로컬 날짜 (회귀 방지) ────────────────────


async def test_same_evening_occurrence_is_materialized_when_server_date_is_ahead(
    async_client: AsyncClient, staff_at_store: dict, staff_headers: dict, monkeypatch
) -> None:
    """서버 프로세스 날짜가 org 로컬보다 하루 앞서도(운영 컨테이너 UTC + LA 조직의 17:00~24:00)
    **그날 저녁 근무가 저장 즉시 실체화되어 앱/HTMA 에 보인다.**

    회귀 대상: 창 기준일로 `date.today()`(서버 tz)를 쓰면 그 occurrence 가 창 밖으로 밀리고,
    일 1회 catch-up 은 그날 기준으로 다시 돌아 지나간 날짜를 못 살려 영구 누락된다.
    """
    _, today = await _store_now(staff_at_store["store_id"])

    class _AheadDate(date):
        @classmethod
        def today(cls):  # noqa: D401 - 서버가 UTC 라 하루 앞선 상황 재현
            return today + timedelta(days=1)

    for mod in (
        "app.services.fixed_schedule.patterns",
        "app.services.fixed_schedule.materialize",
    ):
        monkeypatch.setattr(f"{mod}.date", _AheadDate)

    await _create_pattern(staff_at_store, start=time(22, 0), end=time(23, 55), start_date=today)

    days = [r.operating_day for r in await _materialized(staff_at_store)]
    assert today in days, "org 로컬 오늘 저녁 근무가 실체화되지 않음"

    resp = await async_client.get(
        f"/api/v1/app/my/schedules?date_from={today}&date_to={today}", headers=staff_headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [it["operating_day"] for it in items] == [today.isoformat()]
