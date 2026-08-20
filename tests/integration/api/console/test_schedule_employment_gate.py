"""퇴사·비활성 직원에게 새 근무를 꽂을 수 없다 (2026-08-19).

발단 — 스케줄 저장 경로에 **고용 상태 검증이 없었다.** 매장 배정은 보고 있었지만
(`USER_NOT_IN_STORE`), 퇴사자·비활성자는 그리드의 `+`·Bulk 빌더·Copy from week·
StaffPicker 어느 쪽으로도 그대로 저장됐다.

규칙(D1): 퇴사일(=마지막 근무일) **당일까지는 허용**, 다음날부터 차단.
퇴사일이 없는 비활성자는 판정 기준이 없으므로 전 날짜 차단.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.core import schedule_codes as codes
from app.database import async_session
from app.models.attendance import Attendance
from app.models.org_member import OrgMember
from app.models.schedule import Schedule
from app.models.user import User
from app.models.user_store import UserStore

pytestmark = pytest.mark.asyncio

CREATE_URL = "/api/v1/console/schedules"
LEFT_ON = date.today() + timedelta(days=200)      # 퇴사일 = 마지막 근무일
AFTER = LEFT_ON + timedelta(days=1)               # 그 다음날 — 막혀야 한다
_DAYS = [LEFT_ON, AFTER]


def _payload(user_id, store_id, day: date, **over):
    base = {
        "user_id": str(user_id),
        "store_id": str(store_id),
        "work_date": day.isoformat(),
        "start_time": "09:00",
        "end_time": "17:00",
        "status": "confirmed",
        "force": True,  # 경고는 확인하고 넘어간다 — 이 테스트가 보는 건 에러뿐
    }
    base.update(over)
    return base


def _codes_of(resp) -> set[str]:
    detail = resp.json().get("detail") or resp.json().get("error") or {}
    return {it.get("code") for it in detail.get("errors", [])}


@pytest_asyncio.fixture
async def assigned_staff(test_user, test_store_id) -> AsyncIterator[dict]:
    """매장 배정된 직원 — 고용 상태만 바꿔가며 본다. 상태는 끝나고 원복."""
    async with async_session() as db:
        await db.execute(delete(UserStore).where(
            UserStore.user_id == test_user["id"], UserStore.store_id == test_store_id,
        ))
        db.add(UserStore(
            user_id=test_user["id"], store_id=test_store_id, is_work_assignment=True,
        ))
        user = await db.scalar(select(User).where(User.id == test_user["id"]))
        member = await db.scalar(select(OrgMember).where(
            OrgMember.user_id == test_user["id"],
            OrgMember.organization_id == user.organization_id,
        ))
        before = {
            "is_active": user.is_active,
            "member_status": member.status if member else None,
            "termination_date": member.termination_date if member else None,
        }
        await db.commit()
    try:
        yield {**test_user, "store_id": test_store_id, "org_id": user.organization_id}
    finally:
        async with async_session() as db:
            u = await db.scalar(select(User).where(User.id == test_user["id"]))
            u.is_active = before["is_active"]
            m = await db.scalar(select(OrgMember).where(
                OrgMember.user_id == test_user["id"],
                OrgMember.organization_id == u.organization_id,
            ))
            if m is not None:
                m.status = before["member_status"]
                m.termination_date = before["termination_date"]
            await db.execute(delete(Attendance).where(
                Attendance.user_id == test_user["id"], Attendance.work_date.in_(_DAYS),
            ))
            await db.execute(delete(Schedule).where(
                Schedule.user_id == test_user["id"], Schedule.operating_day.in_(_DAYS),
            ))
            await db.execute(delete(UserStore).where(
                UserStore.user_id == test_user["id"], UserStore.store_id == test_store_id,
            ))
            await db.commit()


async def _set_employment(staff, *, is_active: bool, status: str | None, term: date | None):
    async with async_session() as db:
        u = await db.scalar(select(User).where(User.id == staff["id"]))
        u.is_active = is_active
        m = await db.scalar(select(OrgMember).where(
            OrgMember.user_id == staff["id"],
            OrgMember.organization_id == staff["org_id"],
        ))
        if m is not None and status is not None:
            m.status = status
            m.termination_date = term
        await db.commit()


class TestTerminated:
    async def test_day_after_last_working_day_is_blocked(
        self, async_client: AsyncClient, admin_headers, assigned_staff
    ):
        await _set_employment(
            assigned_staff, is_active=False, status="terminated", term=LEFT_ON
        )
        resp = await async_client.post(
            CREATE_URL, json=_payload(assigned_staff["id"], assigned_staff["store_id"], AFTER),
            headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        assert codes.USER_TERMINATED_BEFORE_DATE in _codes_of(resp)

    async def test_last_working_day_itself_is_allowed(
        self, async_client: AsyncClient, admin_headers, assigned_staff
    ):
        """퇴사일 당일까지는 근무로 본다 — 여기까지 막으면 소급 입력이 불가능해진다."""
        await _set_employment(
            assigned_staff, is_active=False, status="terminated", term=LEFT_ON
        )
        resp = await async_client.post(
            CREATE_URL, json=_payload(assigned_staff["id"], assigned_staff["store_id"], LEFT_ON),
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text

    async def test_force_does_not_break_through(
        self, async_client: AsyncClient, admin_headers, assigned_staff
    ):
        """확인(force)은 경고를 넘기는 장치지 에러를 뚫는 장치가 아니다."""
        await _set_employment(
            assigned_staff, is_active=False, status="terminated", term=LEFT_ON
        )
        resp = await async_client.post(
            CREATE_URL,
            json=_payload(assigned_staff["id"], assigned_staff["store_id"], AFTER, force=True),
            headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text


class TestInactiveWithoutTerminationDate:
    async def test_blocked_on_every_date(
        self, async_client: AsyncClient, admin_headers, assigned_staff
    ):
        await _set_employment(assigned_staff, is_active=False, status="active", term=None)
        for day in (LEFT_ON, AFTER):
            resp = await async_client.post(
                CREATE_URL, json=_payload(assigned_staff["id"], assigned_staff["store_id"], day),
                headers=admin_headers,
            )
            assert resp.status_code == 400, resp.text
            assert codes.USER_NOT_EMPLOYED in _codes_of(resp)


class TestActiveControl:
    async def test_active_staff_still_saves(
        self, async_client: AsyncClient, admin_headers, assigned_staff
    ):
        """게이트가 정상 배정까지 막지 않는다는 대조군."""
        await _set_employment(assigned_staff, is_active=True, status="active", term=None)
        resp = await async_client.post(
            CREATE_URL, json=_payload(assigned_staff["id"], assigned_staff["store_id"], AFTER),
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text


class TestReassign:
    async def test_changing_staff_to_a_terminated_person_is_blocked(
        self, async_client: AsyncClient, admin_headers, assigned_staff
    ):
        """담당자 교체(ChangeStaffModal) 경로도 같은 게이트를 지난다."""
        await _set_employment(assigned_staff, is_active=True, status="active", term=None)
        created = await async_client.post(
            CREATE_URL, json=_payload(assigned_staff["id"], assigned_staff["store_id"], AFTER),
            headers=admin_headers,
        )
        assert created.status_code == 201, created.text
        entry_id = created.json()["id"]

        await _set_employment(
            assigned_staff, is_active=False, status="terminated", term=LEFT_ON
        )
        resp = await async_client.patch(
            f"{CREATE_URL}/{entry_id}",
            json={"user_id": str(assigned_staff["id"]), "force": True},
            headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        assert codes.USER_TERMINATED_BEFORE_DATE in _codes_of(resp)


class TestRosterKeepsFilteredStaff:
    """필터로 지목한 사람은 그리드에서 사라지지 않는다 (2026-08-19).

    예전엔 지목을 **교집합**으로 처리해서, 비활성이면서 그 기간에 기록이 없으면
    행이 통째로 없어졌다. 화면엔 이름 칩만 남고 표는 비어 "필터가 고장난" 것처럼 보였다.
    """

    async def test_inactive_staff_with_no_schedules_still_gets_a_row(
        self, async_client: AsyncClient, admin_headers, assigned_staff
    ):
        await _set_employment(assigned_staff, is_active=False, status="active", term=None)
        # 아무 기록도 없는 먼 미래 주간 — fail-open(기간 내 기록) 으로는 절대 안 잡힌다.
        empty_from = LEFT_ON + timedelta(days=400)
        resp = await async_client.get(
            "/api/v1/console/schedules/roster",
            params={
                "date_from": empty_from.isoformat(),
                "date_to": (empty_from + timedelta(days=6)).isoformat(),
                "granularity": "week",
                "store_ids": str(assigned_staff["store_id"]),
                "staff_ids": str(assigned_staff["id"]),
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        rows = resp.json()["roster"]
        assert [r["user_id"] for r in rows] == [str(assigned_staff["id"])]
        # 행은 있지만 배정은 막혀 있어야 한다 — 보이는 것과 저장 가능한 것은 다른 축이다.
        assert rows[0]["assignable"] is False
        assert rows[0]["has_schedule_in_period"] is False

    async def test_terminated_staff_row_carries_the_last_working_day(
        self, async_client: AsyncClient, admin_headers, assigned_staff
    ):
        await _set_employment(
            assigned_staff, is_active=False, status="terminated", term=LEFT_ON
        )
        empty_from = LEFT_ON + timedelta(days=400)
        resp = await async_client.get(
            "/api/v1/console/schedules/roster",
            params={
                "date_from": empty_from.isoformat(),
                "date_to": (empty_from + timedelta(days=6)).isoformat(),
                "granularity": "week",
                "store_ids": str(assigned_staff["store_id"]),
                "staff_ids": str(assigned_staff["id"]),
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        row = resp.json()["roster"][0]
        assert row["assignable"] is True
        assert row["assignable_until"] == LEFT_ON.isoformat()


class TestTipEntryGate:
    """팁도 같은 게이트를 지난다 (2026-08-19).

    화면에서 후보를 좁히는 것만으로는 API 직접 호출을 막지 못한다. 날짜 기준이라
    재직 중이던 **과거 날짜**의 팁 입력은 그대로 되어야 한다 — 그게 이 규칙의 핵심이다.
    """

    TIP_URL = "/api/v1/console/tips/entries"

    def _tip_payload(self, staff, day):
        return {
            "employee_id": str(staff["id"]),
            "store_id": str(staff["store_id"]),
            "date": day.isoformat(),
            "card_tips": "10.00",
            "cash_tips_kept": "0.00",
            "comment": "test",
            "distributions": [],
        }

    async def test_after_last_working_day_is_blocked(
        self, async_client: AsyncClient, admin_headers, assigned_staff
    ):
        await _set_employment(
            assigned_staff, is_active=False, status="terminated", term=LEFT_ON
        )
        resp = await async_client.post(
            self.TIP_URL, json=self._tip_payload(assigned_staff, AFTER),
            headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        assert "last working day" in resp.text

    async def test_deactivated_without_date_is_blocked(
        self, async_client: AsyncClient, admin_headers, assigned_staff
    ):
        await _set_employment(assigned_staff, is_active=False, status="active", term=None)
        resp = await async_client.post(
            self.TIP_URL, json=self._tip_payload(assigned_staff, LEFT_ON),
            headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        assert "no longer active" in resp.text
