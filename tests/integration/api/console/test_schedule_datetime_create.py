"""API integration — 스케줄 create 의 datetime(start_at/end_at) 전환기 경로.

POST /api/v1/console/schedules 가 구(work_date+HH:MM)/신(operating_day+ISO) 입력을
모두 받아 두 인코딩을 저장하고 응답에 동시 노출하는지 검증.
"""
from __future__ import annotations

from datetime import date, timedelta
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
    """test_user 를 test store 에 work-assignment 로 배정 + 미래 스케줄 정리.

    매장 경계를 **06:00** 으로 둔다. 공용 테스트 매장의 기본값 00:00 에서는
    "영업일 라벨과 시작 달력일이 다른(+1d) 근무" 자체가 성립하지 않는다 —
    영업일 D 의 구간이 `[D 00:00, D+1 00:00)` 이라 D+1 의 어떤 시각도 구간 밖이다.
    이 파일은 그 +1d 인코딩을 검증하므로 경계가 자정이 아니어야 한다.
    """
    from app.models.organization import Store as _Store

    async with async_session() as db:
        _store = await db.get(_Store, test_store_id)
        _orig_ds = _store.day_start_time
        _store.day_start_time = {"all": "06:00"}
        await db.commit()
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
            _store = await db.get(_Store, test_store_id)
            _store.day_start_time = _orig_ds
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
        # 경계 06:00 이라 01:00 시작의 자동 판정이 곧 12/5 다 — 사람이 고를 것도 없다.
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
    """전날 마감조(익일 새벽 종료)와 다음날 새벽조의 물리 겹침을 검출해야 함.

    D9 — 겹침은 **경고**다(에러 아님). 확인 없이 저장하려 하면 409,
    force:true 로 재요청하면 저장된다. 한 사람이 두 역할을 겹쳐 맡는 상황이
    실제로 있을 수 있어 에러로 두면 표현할 방법이 없었다.
    """
    # 12/4 22:00 → 12/5 02:00 (overnight)
    r1 = await async_client.post(CREATE_URL, json={
        "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        "work_date": FUTURE.isoformat(), "start_time": "22:00", "end_time": "02:00",
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    assert r1.status_code == 201, r1.text
    # 12/5 01:00~09:00 — 12/5 01:00~02:00 구간이 물리적으로 겹침.
    # 경계 06:00 이므로 01:00 시작은 **영업일 12/4 의 새벽조**다(12/5 로 두면 12/6 01:00 이 된다).
    body = {
        "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        "work_date": FUTURE.isoformat(), "start_time": "01:00", "end_time": "09:00",
        "status": "confirmed", "force": False,
    }
    r2 = await async_client.post(CREATE_URL, json=body, headers=admin_headers)
    assert r2.status_code == 409, r2.text
    detail = r2.json()["detail"]
    assert detail["code"] == "SCHEDULE_WARNINGS_UNCONFIRMED", detail
    assert any(w["code"] == "OVERLAPPING_SCHEDULE" for w in detail["warnings"]), detail
    assert detail["retry"] == {"force": True}, detail

    # 확인 후 진행 — force 로 재요청하면 저장된다
    r3 = await async_client.post(CREATE_URL, json={**body, "force": True}, headers=admin_headers)
    assert r3.status_code == 201, r3.text


async def test_early_morning_explicit_no_false_overlap(async_client, admin_headers, staff_assigned):
    """실제 instant 가 다르면 겹침이 아니다 — 하루 차이 나는 두 새벽조.

    예전엔 "같은 영업일 라벨 + 다른 달력일" 로 이 상황을 만들었는데, 그 모양은 이제
    성립하지 않는다(두 후보 중 하나는 반드시 자기 영업일 구간 밖이다). 영업일을 하루씩
    나눠 같은 취지를 검증한다 — 겹침 판정은 라벨이 아니라 **물리 시각**으로 한다.
    """
    r1 = await async_client.post(CREATE_URL, json={
        "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        "operating_day": (FUTURE - timedelta(days=1)).isoformat(),
        "start_at": f"{FUTURE.isoformat()}T01:00", "end_at": f"{FUTURE.isoformat()}T05:00",
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    assert r1.status_code == 201, r1.text
    # 하루 뒤 같은 시각 — 물리적으로 안 겹침 → 성공해야 한다
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


async def test_start_date_outside_window_is_an_error_on_validate(async_client, admin_headers, staff_assigned):
    """경계(06:00) 이후 시작인데 +1일 = 자기 영업일 구간 밖 → 프리플라이트가 **에러**로 답한다.

    예전엔 경고였고 `force` 로 넘길 수 있었다. 그렇게 저장된 행은 출근 시점에 후보로
    잡히지 않아 현장에서 쓸 수 없다(2026-08 사고). 이제 확인으로도 못 넘긴다.
    """
    resp = await async_client.post(f"{CREATE_URL}/validate", json={
        "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        "operating_day": FUTURE.isoformat(),
        "start_at": "2026-12-05T07:00", "end_at": "2026-12-05T15:00",  # 경계 이후인데 +1일
        "force": False,
    }, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    err = next(e for e in body["errors"] if e["code"] == "START_DATE_MISMATCH")
    # 사람이 다음에 뭘 바꿔야 하는지 — 시작 날짜가 아니라 영업일이다.
    assert err["params"]["suggested_operating_day"] == "2026-12-05"


async def test_legacy_time_edit_recalculates_date(async_client, admin_headers, staff_assigned):
    """구 클라이언트(start_time만 전송)가 시각을 바꾸면 **날짜를 다시 파생**한다.

    예전 계약은 반대였다 — 기존 +1d 오프셋을 보존했다. 그게 2026-08 오염의 생성 경로였다
    (경계 11:00 매장에서 09:00(+1d) shift 의 시각만 17:00 으로 바꾸면 +1d 가 남아 하루 뒤에 저장).
    이제는 새 시각에서 다시 계산하되, 날짜가 움직이면 START_DATE_RECALCULATED 경고로 확인을 받는다.
    """
    create = await async_client.post(CREATE_URL, json={
        "user_id": str(staff_assigned["id"]), "store_id": str(staff_assigned["store_id"]),
        "operating_day": FUTURE.isoformat(),
        "start_at": "2026-12-05T01:00", "end_at": "2026-12-05T09:00",
        # 경계 06:00 이라 01:00 시작의 자동 판정이 곧 12/5 다 — 사람이 고를 것도 없다.
        "status": "confirmed", "force": True,
    }, headers=admin_headers)
    assert create.status_code == 201, create.text
    sid = create.json()["id"]
    # 키오스크/벌크식 구 필드 PATCH: 01:00 → 07:00 (날짜를 보낼 수단이 없는 클라이언트).
    # 07:00 은 경계(06:00) 이후라 시작 달력일이 12/5 → 12/4 로 움직인다.
    # 확인 없이 보내면 409 — 날짜가 조용히 움직이지 않는다.
    unconfirmed = await async_client.patch(f"{CREATE_URL}/{sid}", json={
        "start_time": "07:00", "end_time": "15:00",
    }, headers=admin_headers)
    assert unconfirmed.status_code == 409, unconfirmed.text
    warns = unconfirmed.json()["detail"]["warnings"]
    assert any(w["code"] == "START_DATE_RECALCULATED" for w in warns), warns

    # 확인 후 재요청 — 경계 06:00 이후 시각이므로 자동 판정은 영업일 당일이다.
    patch = await async_client.patch(f"{CREATE_URL}/{sid}", json={
        "start_time": "07:00", "end_time": "15:00", "force": True,
    }, headers=admin_headers)
    assert patch.status_code == 200, patch.text
    b = patch.json()
    assert b["start_at"] == f"{FUTURE.isoformat()}T07:00", b
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
