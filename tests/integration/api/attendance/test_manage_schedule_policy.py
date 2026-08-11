"""Phase 1-D — 키오스크의 수정 범위와 기록 (D9-1, D10-1 ~ D10-4).

여기서 고정하는 것:

1. **날짜 제약 없음** — 키오스크가 오늘이 아닌 영업일도 생성/수정/삭제한다(D10-1).
   대신 시각 앵커는 "오늘"이 아니라 **대상 스케줄의 영업일**이어야 한다.
   앵커가 today 로 남아 있으면 어제 스케줄의 시간을 고치는 순간 날짜가 오늘로 끌려온다.
2. **무조건 force 금지** — 겹침 같은 경고는 409 로 돌아오고, 확인(force:true) 후에만 저장된다(D9-1).
3. **급여 기간 잠금이 항상 이긴다** — 잠긴 기간으로 옮기는 수정("into")과
   잠긴 기간 안의 수정·삭제("out_of") 둘 다 차단되고, 코드+파라미터로 구분된다(D10-3).
4. **삭제는 근태를 지우지 않는다** — attendance 는 cancelled 로 남고 이력에 기록된다(D10-2).
   예전 키오스크 삭제는 hard delete 라 breaks/corrections 까지 CASCADE 로 소멸했다.
5. **승인 설정을 키오스크도 따른다** — 기본은 꺼짐이라 동작 변화가 없어야 하고,
   켜면 SV 생성분이 requested 로 남는다(D10-4).

주의 — 이 파일의 테스트는 **매장 설정과 매니저 배정을 바꾼다.** 반드시 원복한다
(과거에 설정이 남아 다른 테스트를 오염시킨 사고가 있었다).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.core import schedule_codes as codes
from app.core.app_version_compat import APP_VERSION_HEADER
from app.database import async_session
from app.models.attendance import Attendance, AttendanceCorrection
from app.models.payroll import PayPeriod
from app.models.schedule import Schedule
from app.models.settings import StoreSetting
from app.models.user_store import UserStore
from app.services.payroll_period_service import prev_period_bounds


pytestmark = pytest.mark.asyncio

MANAGE_URL = "/api/v1/attendance/manage/schedules"
APPROVAL_KEY = "schedule.approval_required"


# ── 헬퍼 ───────────────────────────────────────────────────────────


async def _ensure_user_store(user_id: UUID, store_id: UUID, *, is_manager: bool) -> bool:
    """user_stores idempotent ensure. 새로 만들었으면 True (teardown 이 되돌릴 대상)."""
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
            await db.commit()
            return True
        if is_manager and not existing.is_manager:
            existing.is_manager = True
        await db.commit()
        return False


async def _manage_token(
    async_client: AsyncClient, device_auth_headers: dict, pin: str,
) -> dict:
    resp = await async_client.post(
        "/api/v1/attendance/manage/session",
        headers=device_auth_headers,
        json={"pin": pin},
    )
    assert resp.status_code == 201, resp.text
    return {**device_auth_headers, "X-Manage-Session": resp.json()["manage_token"]}


@pytest_asyncio.fixture
async def gm_manage_headers(
    async_client: AsyncClient, device_auth_headers: dict, test_users: dict,
    test_store_id: UUID,
) -> dict:
    """GM(testgm) 의 manage 세션 헤더. GM 은 승인 워크플로 다운그레이드 대상이 아니다."""
    await _ensure_user_store(test_users["testgm"]["id"], test_store_id, is_manager=True)
    return await _manage_token(
        async_client, device_auth_headers, test_users["testgm"]["clockin_pin"],
    )


@pytest_asyncio.fixture
async def staff_in_store(test_user: dict, test_store_id: UUID) -> None:
    """teststaff 를 매장에 배정 — 스케줄 검증(USER_NOT_IN_STORE) 통과용."""
    await _ensure_user_store(test_user["id"], test_store_id, is_manager=False)


async def _create(
    async_client: AsyncClient, headers: dict, user_id: UUID, **body,
):
    payload = {"user_id": str(user_id), **body}
    return await async_client.post(MANAGE_URL, headers=headers, json=payload)


def _new_client(headers: dict) -> dict:
    """`force` 를 스스로 보낼 수 있는 **신버전 HTMA** 를 가장한 헤더.

    서버는 `X-App-Version` 이 없으면 구버전으로 보고 force 를 박아준다
    (구버전엔 409 확인 모달이 없어서 겹침이 원인 모를 저장 실패로 보인다).
    확인 흐름 자체를 검증하려면 신버전이어야 한다.
    """
    return {**headers, APP_VERSION_HEADER: "1.0.17+38"}


# ── D10-1. 날짜 제약 제거 ──────────────────────────────────────────


async def test_create_on_past_operating_day(
    async_client: AsyncClient, gm_manage_headers: dict, test_user: dict,
    staff_in_store: None,
) -> None:
    """오늘이 아닌 과거 영업일에도 생성된다 — 예전엔 오늘만 허용했다(D10-1)."""
    target = datetime.now(timezone.utc).date() - timedelta(days=5)
    resp = await _create(
        async_client, gm_manage_headers, test_user["id"],
        start_time="10:00", end_time="14:00", operating_day=target.isoformat(),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["operating_day"] == target.isoformat()
    # 앵커 확인 — 시각이 오늘이 아니라 대상 영업일에 붙어야 한다.
    assert body["start_at"] == f"{target.isoformat()}T10:00"
    assert body["end_at"] == f"{target.isoformat()}T14:00"


async def test_update_past_schedule_keeps_operating_day_anchor(
    async_client: AsyncClient, gm_manage_headers: dict, test_user: dict,
    staff_in_store: None,
) -> None:
    """과거 스케줄의 시간만 고쳐도 날짜가 오늘로 끌려오지 않는다."""
    target = datetime.now(timezone.utc).date() - timedelta(days=3)
    created = await _create(
        async_client, gm_manage_headers, test_user["id"],
        start_time="09:00", end_time="13:00", operating_day=target.isoformat(),
    )
    assert created.status_code == 201, created.text
    sid = created.json()["schedule_id"]

    resp = await async_client.patch(
        f"{MANAGE_URL}/{sid}", headers=gm_manage_headers,
        json={"start_time": "09:30", "end_time": "13:30"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["start_at"] == f"{target.isoformat()}T09:30"
    assert body["end_at"] == f"{target.isoformat()}T13:30"
    assert body["operating_day"] == target.isoformat()


async def test_delete_past_schedule_allowed(
    async_client: AsyncClient, gm_manage_headers: dict, test_user: dict,
    staff_in_store: None,
) -> None:
    """과거 스케줄 삭제도 허용 — soft delete(status='deleted') 로 남는다."""
    target = datetime.now(timezone.utc).date() - timedelta(days=4)
    created = await _create(
        async_client, gm_manage_headers, test_user["id"],
        start_time="10:00", end_time="12:00", operating_day=target.isoformat(),
    )
    assert created.status_code == 201, created.text
    sid = created.json()["schedule_id"]

    resp = await async_client.delete(f"{MANAGE_URL}/{sid}", headers=gm_manage_headers)
    assert resp.status_code == 204, resp.text

    async with async_session() as db:
        sched = await db.scalar(select(Schedule).where(Schedule.id == UUID(sid)))
        assert sched is not None, "삭제는 soft delete 여야 한다"
        assert sched.status == "deleted"


# ── D9-1. 무조건 force 제거 → 409 확인 흐름 ────────────────────────


async def test_create_overlap_returns_409_until_confirmed(
    async_client: AsyncClient, gm_manage_headers: dict, test_user: dict,
    staff_in_store: None,
) -> None:
    """겹치는 스케줄은 409(경고 미확인) → force:true 재요청으로 저장(D9-1).

    신버전 클라이언트 기준이다 — 구버전은 어댑터가 force 로 취급한다(아래 별도 테스트).
    """
    gm_manage_headers = _new_client(gm_manage_headers)
    target = datetime.now(timezone.utc).date() - timedelta(days=6)
    first = await _create(
        async_client, gm_manage_headers, test_user["id"],
        start_time="10:00", end_time="14:00", operating_day=target.isoformat(),
    )
    assert first.status_code == 201, first.text

    conflict = await _create(
        async_client, gm_manage_headers, test_user["id"],
        start_time="12:00", end_time="16:00", operating_day=target.isoformat(),
    )
    assert conflict.status_code == 409, conflict.text
    detail = conflict.json()["detail"]
    assert detail["code"] == codes.SCHEDULE_WARNINGS_UNCONFIRMED
    assert detail["retry"] == {"force": True}
    assert codes.OVERLAPPING_SCHEDULE in [w["code"] for w in detail["warnings"]]

    confirmed = await _create(
        async_client, gm_manage_headers, test_user["id"],
        start_time="12:00", end_time="16:00", operating_day=target.isoformat(),
        force=True,
    )
    assert confirmed.status_code == 201, confirmed.text


async def test_update_overlap_returns_409_until_confirmed(
    async_client: AsyncClient, gm_manage_headers: dict, test_user: dict,
    staff_in_store: None,
) -> None:
    """수정 경로도 같다 — 예전엔 라우터가 force 를 박아 경고가 사라졌다."""
    gm_manage_headers = _new_client(gm_manage_headers)
    target = datetime.now(timezone.utc).date() - timedelta(days=7)
    first = await _create(
        async_client, gm_manage_headers, test_user["id"],
        start_time="08:00", end_time="10:00", operating_day=target.isoformat(),
    )
    assert first.status_code == 201, first.text
    second = await _create(
        async_client, gm_manage_headers, test_user["id"],
        start_time="15:00", end_time="17:00", operating_day=target.isoformat(),
    )
    assert second.status_code == 201, second.text
    sid = second.json()["schedule_id"]

    # 두 번째를 첫 번째와 겹치게 옮긴다
    resp = await async_client.patch(
        f"{MANAGE_URL}/{sid}", headers=gm_manage_headers,
        json={"start_time": "09:00", "end_time": "11:00"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == codes.SCHEDULE_WARNINGS_UNCONFIRMED

    forced = await async_client.patch(
        f"{MANAGE_URL}/{sid}", headers=gm_manage_headers,
        json={"start_time": "09:00", "end_time": "11:00", "force": True},
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["start_time"] == "09:00"


# ── D10-3. 급여 기간 잠금 ──────────────────────────────────────────


@pytest_asyncio.fixture
async def locked_period(
    seed_organization: dict, test_store_id: UUID,
) -> AsyncIterator[dict]:
    """직전 반월(항상 완전히 과거)을 confirmed 로 잠근다. pay_periods 는 여기서 정리."""
    today = datetime.now(timezone.utc).date()
    start, end = prev_period_bounds(today)
    async with async_session() as db:
        await db.execute(delete(PayPeriod).where(PayPeriod.store_id == test_store_id))
        db.add(PayPeriod(
            organization_id=seed_organization["id"],
            store_id=test_store_id,
            start_date=start,
            end_date=end,
            status="confirmed",
        ))
        await db.commit()
    yield {"locked_date": start, "unlocked_date": end + timedelta(days=1)}
    async with async_session() as db:
        await db.execute(delete(PayPeriod).where(PayPeriod.store_id == test_store_id))
        await db.commit()


def _assert_locked(resp, direction: str) -> None:
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == codes.PAY_PERIOD_LOCKED
    issue = detail["errors"][0]
    assert issue["code"] == codes.PAY_PERIOD_LOCKED
    assert issue["params"]["direction"] == direction
    # 확인 후 진행(force)으로 넘길 수 있는 것처럼 보이면 안 된다 — 잠금은 에러다.
    assert "retry" not in detail


async def test_move_into_locked_period_blocked(
    async_client: AsyncClient, gm_manage_headers: dict, test_user: dict,
    staff_in_store: None, locked_period: dict,
) -> None:
    """잠기지 않은 스케줄을 잠긴 기간으로 옮기는 수정 → direction="into"."""
    created = await _create(
        async_client, gm_manage_headers, test_user["id"],
        start_time="10:00", end_time="14:00",
        operating_day=locked_period["unlocked_date"].isoformat(),
    )
    assert created.status_code == 201, created.text
    sid = created.json()["schedule_id"]

    resp = await async_client.patch(
        f"{MANAGE_URL}/{sid}", headers=gm_manage_headers,
        json={"operating_day": locked_period["locked_date"].isoformat()},
    )
    _assert_locked(resp, "into")


async def test_create_into_locked_period_blocked(
    async_client: AsyncClient, gm_manage_headers: dict, test_user: dict,
    staff_in_store: None, locked_period: dict,
) -> None:
    """잠긴 기간에 새로 만드는 것도 같은 코드로 차단된다."""
    resp = await _create(
        async_client, gm_manage_headers, test_user["id"],
        start_time="10:00", end_time="14:00",
        operating_day=locked_period["locked_date"].isoformat(),
    )
    _assert_locked(resp, "into")


async def test_edit_and_delete_inside_locked_period_blocked(
    async_client: AsyncClient, gm_manage_headers: dict, test_user: dict,
    seed_organization: dict, test_store_id: UUID, staff_in_store: None,
    locked_period: dict,
) -> None:
    """잠긴 기간 안의 근무를 고치거나 빼내는 수정 → direction="out_of".

    잠긴 뒤에 생긴 스케줄은 API 로 만들 수 없으므로(위 테스트가 그걸 막는다)
    행을 직접 넣는다 — 확정 전에 만들어져 있던 스케줄의 상황 재현.
    """
    locked_date: date = locked_period["locked_date"]
    async with async_session() as db:
        sched = Schedule(
            organization_id=seed_organization["id"],
            user_id=test_user["id"],
            store_id=test_store_id,
            operating_day=locked_date,
            start_at=datetime.combine(locked_date, time(9, 0)),
            end_at=datetime.combine(locked_date, time(17, 0)),
            status="confirmed",
        )
        db.add(sched)
        await db.commit()
        sid = sched.id

    edit = await async_client.patch(
        f"{MANAGE_URL}/{sid}", headers=gm_manage_headers,
        json={"start_time": "10:00", "end_time": "17:00"},
    )
    _assert_locked(edit, "out_of")

    removal = await async_client.delete(f"{MANAGE_URL}/{sid}", headers=gm_manage_headers)
    _assert_locked(removal, "out_of")


# ── D10-2. 삭제해도 근태는 남고 기록된다 ───────────────────────────


async def test_delete_keeps_attendance_and_records_history(
    async_client: AsyncClient, gm_manage_headers: dict, device_auth_headers: dict,
    test_user: dict, staff_in_store: None,
) -> None:
    """키오스크 삭제가 attendance 를 지우지 않는다 — cancelled 로 남고 이력이 생긴다.

    예전엔 hard delete 라 breaks/corrections 까지 CASCADE 로 사라졌다.
    근태는 급여의 근거 자료이므로 "사라졌다는 사실" 자체가 남아야 한다(D10-2).
    """
    now = datetime.now(timezone.utc)
    start = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    created = await _create(
        async_client, gm_manage_headers, test_user["id"],
        start_time=start.strftime("%H:%M"),
        end_time=(start + timedelta(hours=4)).strftime("%H:%M"),
    )
    assert created.status_code == 201, created.text
    sid = created.json()["schedule_id"]

    clock_in = await async_client.post(
        "/api/v1/attendance/clock-in",
        headers=device_auth_headers,
        json={"user_id": str(test_user["id"]), "pin": test_user["clockin_pin"]},
    )
    assert clock_in.status_code == 200, clock_in.text

    resp = await async_client.delete(f"{MANAGE_URL}/{sid}", headers=gm_manage_headers)
    assert resp.status_code == 204, resp.text

    async with async_session() as db:
        att = await db.scalar(
            select(Attendance).where(Attendance.schedule_id == UUID(sid))
        )
        assert att is not None, "attendance 행이 사라지면 안 된다"
        assert att.status == "cancelled"
        assert att.clock_in is not None, "출근 사실은 보존된다"
        history = (await db.execute(
            select(AttendanceCorrection).where(
                AttendanceCorrection.attendance_id == att.id,
                AttendanceCorrection.field_name == "status",
            )
        )).scalars().all()
        assert any(h.corrected_value == "cancelled" for h in history), (
            "취소 사실이 Activity History 에 남아야 한다"
        )


# ── D10-4. 키오스크도 승인 설정을 따른다 ───────────────────────────


@pytest_asyncio.fixture
async def sv_manage_headers(
    async_client: AsyncClient, device_auth_headers: dict, test_users: dict,
    test_store_id: UUID,
) -> AsyncIterator[dict]:
    """SV(testsv) 의 manage 세션 헤더. 승인 워크플로 다운그레이드 대상은 SV 이하다.

    매니저 배정은 테스트가 만든 것만 원복한다 — 이미 있던 배정을 지우면 다른
    테스트의 전제를 부순다.
    """
    sv = test_users["testsv"]
    created = await _ensure_user_store(sv["id"], test_store_id, is_manager=True)
    headers = await _manage_token(async_client, device_auth_headers, sv["clockin_pin"])
    yield headers
    if created:
        async with async_session() as db:
            await db.execute(delete(UserStore).where(
                UserStore.user_id == sv["id"], UserStore.store_id == test_store_id,
            ))
            await db.commit()


@pytest_asyncio.fixture
async def approval_required(test_store_id: UUID) -> AsyncIterator[None]:
    """schedule.approval_required 를 매장 수준에서 켠다. 끝나면 **행을 지워** 원복."""
    from app.main import seed_settings_registry

    await seed_settings_registry()
    async with async_session() as db:
        db.add(StoreSetting(store_id=test_store_id, key=APPROVAL_KEY, value=True))
        await db.commit()
    yield
    async with async_session() as db:
        await db.execute(delete(StoreSetting).where(
            StoreSetting.store_id == test_store_id, StoreSetting.key == APPROVAL_KEY,
        ))
        await db.commit()


async def test_kiosk_create_confirmed_when_approval_off(
    async_client: AsyncClient, sv_manage_headers: dict, test_user: dict,
    staff_in_store: None,
) -> None:
    """기본값(승인 꺼짐)에서는 SV 키오스크 생성이 그대로 confirmed — 동작 변화 없음."""
    target = datetime.now(timezone.utc).date() - timedelta(days=8)
    resp = await _create(
        async_client, sv_manage_headers, test_user["id"],
        start_time="10:00", end_time="14:00", operating_day=target.isoformat(),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "confirmed"


async def test_kiosk_create_requested_when_approval_on(
    async_client: AsyncClient, sv_manage_headers: dict, test_user: dict,
    staff_in_store: None, approval_required: None,
) -> None:
    """승인이 켜진 조직에서는 키오스크 생성도 requested 로 남는다(D10-4).

    예전엔 라우터가 생성 직후 강제로 confirm 해서 이 경로만 승인 절차를 우회했다.
    """
    target = datetime.now(timezone.utc).date() - timedelta(days=9)
    resp = await _create(
        async_client, sv_manage_headers, test_user["id"],
        start_time="10:00", end_time="14:00", operating_day=target.isoformat(),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "requested"
