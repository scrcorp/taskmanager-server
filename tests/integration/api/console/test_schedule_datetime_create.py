"""API integration — 스케줄 create 의 datetime(start_at/end_at) 전환기 경로.

POST /api/v1/console/schedules 가 구(work_date+HH:MM)/신(operating_day+ISO) 입력을
모두 받아 두 인코딩을 저장하고 응답에 동시 노출하는지 검증.
"""
from __future__ import annotations

from datetime import date
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.database import async_session
from app.models.attendance import Attendance
from app.models.schedule import Schedule
from app.models.user_store import UserStore

pytestmark = pytest.mark.asyncio

CREATE_URL = "/api/v1/console/schedules"
FUTURE = date(2026, 12, 4)


@pytest_asyncio.fixture
async def staff_assigned(test_user, test_store_id) -> AsyncIterator[dict]:
    """test_user 를 test store 에 work-assignment 로 배정 + 미래 스케줄 정리."""
    async with async_session() as db:
        await db.execute(delete(UserStore).where(
            UserStore.user_id == test_user["id"], UserStore.store_id == test_store_id,
        ))
        db.add(UserStore(
            user_id=test_user["id"], store_id=test_store_id,
            is_work_assignment=True,
        ))
        await db.commit()
    info = {**test_user, "store_id": test_store_id}
    try:
        yield info
    finally:
        async with async_session() as db:
            # attendance 먼저 삭제 — schedules 삭제 시 SET NULL로 풀리며
            # walk-in 유니크(user, work_date, schedule_id NULL)와 충돌하는 것 방지
            await db.execute(delete(Attendance).where(
                Attendance.user_id == test_user["id"],
                Attendance.work_date.in_([FUTURE, date(2026, 12, 5)]),
            ))
            await db.execute(delete(Schedule).where(
                Schedule.user_id == test_user["id"],
                Schedule.operating_day.in_([FUTURE, date(2026, 12, 5)]),
            ))
            await db.execute(delete(UserStore).where(
                UserStore.user_id == test_user["id"],
                UserStore.store_id == test_store_id,
            ))
            await db.commit()


async def test_create_legacy_fields_populate_datetime(async_client, admin_headers, staff_assigned):
    """구 필드(work_date+HH:MM)로 생성 → 응답에 start_at/end_at/operating_day 채워짐."""
    payload = {
        "user_id": str(staff_assigned["id"]),
        "store_id": str(staff_assigned["store_id"]),
        "work_date": FUTURE.isoformat(),
        "start_time": "09:00", "end_time": "17:00",
        "break_start_time": "12:00", "break_end_time": "12:30",
        "status": "confirmed", "force": True,
    }
    resp = await async_client.post(CREATE_URL, json=payload, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["start_at"] == "2026-12-04T09:00"
    assert body["end_at"] == "2026-12-04T17:00"
    assert body["operating_day"] == "2026-12-04"
    assert body["net_work_minutes"] == 450  # 8h - 30m


async def test_create_new_fields_early_morning(async_client, admin_headers, staff_assigned):
    """신 필드로 자정 이후 근무 생성 — 영업일 12/4, 실제 12/5 01:00~09:00."""
    payload = {
        "user_id": str(staff_assigned["id"]),
        "store_id": str(staff_assigned["store_id"]),
        "operating_day": FUTURE.isoformat(),
        "start_at": "2026-12-05T01:00", "end_at": "2026-12-05T09:00",
        "status": "confirmed", "force": True,
    }
    resp = await async_client.post(CREATE_URL, json=payload, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["operating_day"] == "2026-12-04"     # 영업일 라벨
    assert body["start_at"] == "2026-12-05T01:00"     # 실제 시각
    assert body["end_at"] == "2026-12-05T09:00"
    assert body["net_work_minutes"] == 480

    # DB 에 두 인코딩 모두 저장됐는지 확인
    async with async_session() as db:
        row = (await db.execute(
            select(Schedule).where(Schedule.id == body["id"])
        )).scalar_one()
        assert row.operating_day == FUTURE
        assert row.start_at.isoformat() == "2026-12-05T01:00:00"
        assert row.start_time.isoformat() == "01:00:00"  # 구 컬럼 동기화


async def test_cross_day_overlap_detected(async_client, admin_headers, staff_assigned):
    """전날 마감조(익일 새벽 종료)와 다음날 새벽조의 물리 겹침을 검출해야 함."""
    # 12/4 22:00 → 12/5 02:00 (overnight)
    r1 = await async_client.post(CREATE_URL, json={
        "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        "work_date": FUTURE.isoformat(), "start_time": "22:00", "end_time": "02:00",
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    assert r1.status_code == 201, r1.text
    # 12/5 01:00~09:00 — 12/5 01:00~02:00 구간이 물리적으로 겹침 → 거부
    r2 = await async_client.post(CREATE_URL, json={
        "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        "work_date": "2026-12-05", "start_time": "01:00", "end_time": "09:00",
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    assert r2.status_code == 400, r2.text
    assert "overlap" in r2.text.lower()


async def test_early_morning_explicit_no_false_overlap(async_client, admin_headers, staff_assigned):
    """같은 영업일 라벨이라도 실제 instant가 다르면(당일 01시 vs 익일 01시) 겹침 아님."""
    r1 = await async_client.post(CREATE_URL, json={
        "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        "operating_day": FUTURE.isoformat(),
        "start_at": f"{FUTURE.isoformat()}T01:00", "end_at": f"{FUTURE.isoformat()}T05:00",
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    assert r1.status_code == 201, r1.text
    # 같은 영업일 12/4 라벨, 실제는 12/5 새벽 — 물리적으로 안 겹침 → 성공해야 함
    r2 = await async_client.post(CREATE_URL, json={
        "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        "operating_day": FUTURE.isoformat(),
        "start_at": "2026-12-05T01:00", "end_at": "2026-12-05T05:00",
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    assert r2.status_code == 201, r2.text


async def test_start_date_hard_constraint(async_client, admin_headers, staff_assigned):
    """start 날짜는 영업일 당일 또는 +1일만 — 그 밖은 reject."""
    resp = await async_client.post(CREATE_URL, json={
        "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        "operating_day": FUTURE.isoformat(),
        "start_at": "2026-12-06T01:00", "end_at": "2026-12-06T09:00",  # +2일
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    assert resp.status_code == 400, resp.text
    assert "operating day" in resp.text.lower()


async def test_boundary_warning_on_validate(async_client, admin_headers, staff_assigned):
    """+1일인데 매장 경계(기본 06:00) 이후 시작이면 프리플라이트 경고."""
    resp = await async_client.post(f"{CREATE_URL}/validate", json={
        "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        "operating_day": FUTURE.isoformat(),
        "start_at": "2026-12-05T07:00", "end_at": "2026-12-05T15:00",  # 경계 06:00 이후
        "force": False,
    }, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert any("day boundary" in w for w in body["warnings"]), body


async def test_legacy_time_edit_preserves_next_day_offset(async_client, admin_headers, staff_assigned):
    """구 클라이언트(start_time만 전송)가 새벽근무 시각을 수정해도 +1d가 보존돼야 함."""
    create = await async_client.post(CREATE_URL, json={
        "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        "operating_day": FUTURE.isoformat(),
        "start_at": "2026-12-05T01:00", "end_at": "2026-12-05T09:00",
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    assert create.status_code == 201, create.text
    sid = create.json()["id"]
    # 키오스크/벌크식 구 필드 PATCH: 01:00 → 02:00
    patch = await async_client.patch(f"{CREATE_URL}/{sid}", json={
        "start_time": "02:00", "end_time": "09:00", "force": True,
    }, headers=admin_headers)
    assert patch.status_code == 200, patch.text
    b = patch.json()
    assert b["start_at"] == "2026-12-05T02:00", b  # +1d 보존 (12/4로 안 당겨짐)
    assert b["operating_day"] == FUTURE.isoformat()


async def test_inverted_break_rejected_400(async_client, admin_headers, staff_assigned):
    """역전 브레이크(ISO) — 과지급(net>gross)으로 저장되던 페이로드는 400."""
    resp = await async_client.post(CREATE_URL, json={
        "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        "operating_day": FUTURE.isoformat(),
        "start_at": f"{FUTURE.isoformat()}T09:00", "end_at": f"{FUTURE.isoformat()}T17:00",
        "break_start_at": f"{FUTURE.isoformat()}T14:00", "break_end_at": f"{FUTURE.isoformat()}T13:00",
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    assert resp.status_code == 400, resp.text
    assert "break" in resp.text.lower()


async def test_legacy_wrap_break_outside_rejected_400(async_client, admin_headers, staff_assigned):
    """구 인코딩 오타(break_end 08:00 < start) — 22h 창밖 브레이크로 net=0 저장되던 케이스는 400."""
    resp = await async_client.post(CREATE_URL, json={
        "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        "work_date": FUTURE.isoformat(), "start_time": "09:00", "end_time": "17:00",
        "break_start_time": "10:00", "break_end_time": "08:00",
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    assert resp.status_code == 400, resp.text
    assert "break" in resp.text.lower()


async def test_partial_datetime_update_preserves_break_and_net(
    async_client, admin_headers, staff_assigned
):
    """신 인코딩 부분 PATCH(start_at/end_at만)가 브레이크를 삭제하거나 net을 오염시키면 안 됨."""
    # 브레이크 있는 근무 생성: 09:00~18:00, break 12:00~13:00 → net 480
    create = await async_client.post(CREATE_URL, json={
        "user_id": str(staff_assigned["id"]),
        "store_id": str(staff_assigned["store_id"]),
        "operating_day": FUTURE.isoformat(),
        "start_at": f"{FUTURE.isoformat()}T09:00", "end_at": f"{FUTURE.isoformat()}T18:00",
        "break_start_at": f"{FUTURE.isoformat()}T12:00", "break_end_at": f"{FUTURE.isoformat()}T13:00",
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    assert create.status_code == 201, create.text
    sid = create.json()["id"]
    assert create.json()["net_work_minutes"] == 480  # 9h - 1h break

    # start_at/end_at만 이동(브레이크 필드 생략) → 브레이크 보존, net 유지
    patch = await async_client.patch(f"{CREATE_URL}/{sid}", json={
        "start_at": f"{FUTURE.isoformat()}T10:00", "end_at": f"{FUTURE.isoformat()}T19:00",
        "force": True,
    }, headers=admin_headers)
    assert patch.status_code == 200, patch.text
    b = patch.json()
    assert b["start_at"] == f"{FUTURE.isoformat()}T10:00"
    assert b["break_start_at"] == f"{FUTURE.isoformat()}T12:00"  # 브레이크 보존
    assert b["break_end_at"] == f"{FUTURE.isoformat()}T13:00"
    assert b["net_work_minutes"] == 480  # 9h - 1h break 유지 (오염 없음)


async def test_bulk_update_day_to_dawn_conversion(async_client, admin_headers, staff_assigned):
    """벌크 시간수정이 신 인코딩을 동봉하면 주간→새벽 전환이 표현돼야 함
    (HH:MM만 보내면 기존 오프셋 보존으로 전환 불가 — 콘솔이 경계 규칙으로 조립해 전송)."""
    create = await async_client.post(CREATE_URL, json={
        "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        "operating_day": FUTURE.isoformat(),
        "start_at": f"{FUTURE.isoformat()}T09:00", "end_at": f"{FUTURE.isoformat()}T17:00",
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    assert create.status_code == 201, create.text
    sid = create.json()["id"]

    # 주간 → 새벽 (콘솔 벌크가 보내는 형태: 구+신 동봉, 새벽은 영업일+1)
    resp = await async_client.patch(f"{CREATE_URL}/bulk", json={"updates": [{
        "id": sid, "start_time": "01:00", "end_time": "05:00",
        "operating_day": FUTURE.isoformat(),
        "start_at": "2026-12-05T01:00", "end_at": "2026-12-05T05:00",
    }]}, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    async with async_session() as db:
        row = (await db.execute(select(Schedule).where(Schedule.id == sid))).scalar_one()
    assert row.start_at.isoformat() == "2026-12-05T01:00:00"
    assert row.operating_day == FUTURE  # 영업일 라벨 유지

    # 새벽 → 주간 (오프셋 1이 0으로 돌아와야 함)
    resp = await async_client.patch(f"{CREATE_URL}/bulk", json={"updates": [{
        "id": sid, "start_time": "10:00", "end_time": "18:00",
        "operating_day": FUTURE.isoformat(),
        "start_at": f"{FUTURE.isoformat()}T10:00", "end_at": f"{FUTURE.isoformat()}T18:00",
    }]}, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    async with async_session() as db:
        row = (await db.execute(select(Schedule).where(Schedule.id == sid))).scalar_one()
    assert row.start_at.isoformat() == f"{FUTURE.isoformat()}T10:00:00"
