"""D9 계약 — 에러/경고 코드 체계와 "확인 후 진행".

    | 요청       | 에러 있음 | 경고만 있음        | 깨끗함 |
    |------------|-----------|--------------------|--------|
    | force 없음 | 400 차단  | 409 + warnings     | 저장   |
    | force:true | 400 차단  | 저장 + warnings 응답 | 저장 |

핵심 회귀 방지 대상:
  - 예전엔 `force` 를 인자로 받기만 하고 **아무 데도 쓰지 않았다** (분기 0개).
  - 경고는 **실패했을 때만** 에러 메시지에 섞여 나가고 성공하면 버려졌다.
    그래서 "다음 영업일 소속일 수 있다" 경고를 아무도 본 적이 없다.
  - 문구가 아니라 **코드**로 분기해야 한다 (D9-4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete

from app.core import schedule_codes as codes
from app.database import async_session
from app.models.attendance import Attendance
from app.models.schedule import Schedule
from app.models.user_store import UserStore

pytestmark = pytest.mark.asyncio

CREATE_URL = "/api/v1/console/schedules"
FUTURE = date.today() + timedelta(days=200)
_DAYS = [FUTURE, FUTURE + timedelta(days=3)]


@pytest_asyncio.fixture
async def staff_assigned(test_user, test_store_id) -> AsyncIterator[dict]:
    """test_user 를 매장에 work-assignment 로 배정 + 사용 날짜 정리."""
    async with async_session() as db:
        await db.execute(delete(UserStore).where(
            UserStore.user_id == test_user["id"], UserStore.store_id == test_store_id,
        ))
        db.add(UserStore(
            user_id=test_user["id"], store_id=test_store_id, is_work_assignment=True,
        ))
        await db.commit()
    try:
        yield {**test_user, "store_id": test_store_id}
    finally:
        async with async_session() as db:
            await db.execute(delete(Attendance).where(
                Attendance.user_id == test_user["id"], Attendance.work_date.in_(_DAYS),
            ))
            await db.execute(delete(Schedule).where(
                Schedule.user_id == test_user["id"], Schedule.operating_day.in_(_DAYS),
            ))
            # 배정도 원복한다 — 남겨두면 "이 매장에 없는 직원" 을 전제하는
            # 다른 테스트(IDOR 등)가 조용히 깨진다.
            await db.execute(delete(UserStore).where(
                UserStore.user_id == test_user["id"], UserStore.store_id == test_store_id,
            ))
            await db.commit()


def _zero_duration(staff, **over):
    """0분 근무 — 구 HH:MM 인코딩은 end<=start 를 '다음 날'로 조립하므로
    (09:00~09:00 = 24시간) 명시 ISO 로 같은 instant 를 준다."""
    return _payload(
        staff,
        start_time=None, end_time=None,
        operating_day=FUTURE.isoformat(),
        start_at=f"{FUTURE.isoformat()}T09:00",
        end_at=f"{FUTURE.isoformat()}T09:00",
        **over,
    )


def _payload(staff, **over):
    base = {
        "user_id": str(staff["id"]),
        "store_id": str(staff["store_id"]),
        "work_date": FUTURE.isoformat(),
        "start_time": "09:00",
        "end_time": "17:00",
        "status": "confirmed",
        "force": False,
    }
    base.update(over)
    return base


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestCodeRegistry:
    def test_error_and_warning_sets_are_disjoint(self):
        assert not (codes.ERROR_CODES & codes.WARNING_CODES)

    def test_overlap_is_a_warning_not_an_error(self):
        """D9 — 한 사람이 두 역할을 겹쳐 맡는 상황을 표현할 수 있어야 한다."""
        assert codes.OVERLAPPING_SCHEDULE in codes.WARNING_CODES

    def test_codes_are_upper_snake(self):
        for c in codes.ERROR_CODES | codes.WARNING_CODES:
            assert c == c.upper(), c
            assert " " not in c

    def test_issue_rejects_unregistered_code(self):
        with pytest.raises(ValueError, match="Unregistered"):
            codes.issue("NOT_A_REAL_CODE")

    def test_issue_drops_none_params(self):
        got = codes.issue(codes.ZERO_DURATION, a=1, b=None)
        assert got == {"code": codes.ZERO_DURATION, "params": {"a": 1}}


class TestErrorPath:
    async def test_error_is_400_with_codes(self, async_client: AsyncClient, admin_headers, staff_assigned):
        """0분 근무 — 데이터가 깨지므로 에러."""
        resp = await async_client.post(
            CREATE_URL, json=_zero_duration(staff_assigned), headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == codes.SCHEDULE_INVALID
        assert any(e["code"] == codes.ZERO_DURATION for e in detail["errors"]), detail
        assert detail["message"]  # fallback 문구는 항상 있다

    async def test_force_cannot_bypass_an_error(self, async_client: AsyncClient, admin_headers, staff_assigned):
        """force 는 '확인 후 진행'이지 '검증 무시'가 아니다."""
        resp = await async_client.post(
            CREATE_URL,
            json=_zero_duration(staff_assigned, force=True), headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["code"] == codes.SCHEDULE_INVALID


class TestWarningConfirmFlow:
    async def test_warning_blocks_with_409_then_force_saves(
        self, async_client: AsyncClient, admin_headers, staff_assigned,
    ):
        first = await async_client.post(
            CREATE_URL, json=_payload(staff_assigned, force=True), headers=admin_headers,
        )
        assert first.status_code == 201, first.text

        # 겹치는 스케줄 — 경고만 있으므로 409
        dup = _payload(staff_assigned, start_time="10:00", end_time="16:00")
        resp = await async_client.post(CREATE_URL, json=dup, headers=admin_headers)
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == codes.SCHEDULE_WARNINGS_UNCONFIRMED
        assert any(w["code"] == codes.OVERLAPPING_SCHEDULE for w in detail["warnings"])
        assert detail["retry"] == {"force": True}

        # 확인 후 진행
        confirmed = await async_client.post(
            CREATE_URL, json={**dup, "force": True}, headers=admin_headers,
        )
        assert confirmed.status_code == 201, confirmed.text

    async def test_clean_request_needs_no_force(
        self, async_client: AsyncClient, admin_headers, staff_assigned,
    ):
        resp = await async_client.post(
            CREATE_URL,
            json=_payload(staff_assigned, work_date=(FUTURE + timedelta(days=3)).isoformat()),
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text


class TestPreviewAlwaysReturns200:
    """N3 — 프리뷰는 저장 시도가 아니라 질의다. 409/400 을 쓰지 않는다."""

    async def test_preview_returns_200_with_errors(
        self, async_client: AsyncClient, admin_headers, staff_assigned,
    ):
        resp = await async_client.post(
            f"{CREATE_URL}/validate",
            json=_zero_duration(staff_assigned), headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["valid"] is False
        assert any(e["code"] == codes.ZERO_DURATION for e in body["errors"]), body


class TestBulkDoesNotStopOnWarnings:
    """S1-c — 다건 경로는 경고로 배치를 멈추지 않는다.

    왜 중요한가 (2026-08-14 현장):
      벌크 저장이 첫 경고 항목에서 409 로 통째로 튕겼다. 그런데 `create_entry` 는
      건별로 commit 하므로 **앞 항목은 이미 저장된 상태**였고, 클라이언트에는
      "created: 0" 으로 보고돼 사용자가 재시도하면 중복이 생겼다.
      게다가 겹침·초과근무는 경고(확인 후 진행)라서 애초에 배정을 막으면 안 된다.
    """

    async def test_warning_entry_is_saved_and_reported(
        self, async_client: AsyncClient, admin_headers, staff_assigned,
    ):
        day = FUTURE + timedelta(days=3)
        first = _payload(staff_assigned, work_date=day.isoformat())
        resp = await async_client.post(CREATE_URL, json=first, headers=admin_headers)
        assert resp.status_code == 201, resp.text

        # 같은 사람, 같은 날, 겹치는 시간 = 경고(OVERLAPPING_SCHEDULE)
        overlapping = _payload(
            staff_assigned, work_date=day.isoformat(),
            start_time="10:00", end_time="16:00",
        )
        bulk = await async_client.post(
            f"{CREATE_URL}/bulk",
            json={"entries": [overlapping], "skip_on_conflict": False},
            headers=admin_headers,
        )
        assert bulk.status_code == 200, bulk.text
        body = bulk.json()
        # 저장은 된다 — 경고는 배정을 막지 않는다
        assert body["created"] == 1, body
        # 그리고 무엇을 확인해야 하는지 항목별로 보고된다
        assert body["warnings"], body
        entry = body["warnings"][0]
        assert entry["index"] == 0
        assert any(w["code"] == codes.OVERLAPPING_SCHEDULE for w in entry["warnings"]), entry

    async def test_clean_bulk_reports_no_warnings(
        self, async_client: AsyncClient, admin_headers, staff_assigned,
    ):
        resp = await async_client.post(
            f"{CREATE_URL}/bulk",
            json={"entries": [_payload(staff_assigned)], "skip_on_conflict": False},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"] == 1, body
        assert body["warnings"] == [], body

    async def test_bulk_still_fails_the_entry_on_a_real_error(
        self, async_client: AsyncClient, admin_headers, staff_assigned,
    ):
        """force 는 에러를 뚫지 않는다 — 0분 근무는 벌크에서도 저장되면 안 된다."""
        resp = await async_client.post(
            f"{CREATE_URL}/bulk",
            json={"entries": [_zero_duration(staff_assigned)], "skip_on_conflict": False},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"] == 0, body
        assert body["failed"] == 1, body
