"""API integration — L6 자동퇴근 확인 플로우 (payroll 마감 게이트 ①의 일상 확인).

검증 대상:
    - POST /console/attendances/{id}/confirm-auto-clockout
        happy: confirmed_by/at 기록 + 응답 노출
        400: auto_clocked_out anomaly 없는 record
        멱등: 재확인은 no-op 성공 — 최초 확인자/시각 보존
    - correction-implies-confirm: clock_out 정정 = human-verified → 자동 확인
      (anomaly 'auto_clocked_out' 는 이력이므로 유지)
    - reopen → clock-out 액션 재기록도 확인으로 간주
    - GET list/detail 응답에 auto_clock_out_confirmed_at/by 포함 (미확인 배지용)
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.database import async_session
from app.models.attendance import Attendance

pytestmark = pytest.mark.asyncio

ATT_URL = "/api/v1/console/attendances"


async def _make_auto_clocked_out(
    test_users: dict, test_store_id: UUID, *, with_anomaly: bool = True,
) -> UUID:
    """오늘(UTC) 날짜의 auto_clocked_out attendance row (워크인 형태) 생성."""
    staff = test_users["teststaff"]
    today = datetime.now(timezone.utc).date()
    async with async_session() as db:
        att = Attendance(
            organization_id=staff["organization_id"],
            store_id=test_store_id,
            user_id=staff["id"],
            schedule_id=None,
            work_date=today,
            clock_in=datetime.combine(today, time(0, 30), tzinfo=timezone.utc),
            clock_in_timezone="UTC",
            clock_out=datetime.combine(today, time(8, 30), tzinfo=timezone.utc),
            clock_out_timezone="UTC",
            status="clocked_out",
            anomalies=["auto_clocked_out"] if with_anomaly else None,
            total_work_minutes=480,
        )
        db.add(att)
        await db.commit()
        return att.id


async def _fetch(att_id: UUID) -> Attendance:
    async with async_session() as db:
        return (await db.execute(
            select(Attendance).where(Attendance.id == att_id)
        )).scalar_one()


async def test_confirm_happy_path(async_client, admin_headers, test_users, test_store_id):
    att_id = await _make_auto_clocked_out(test_users, test_store_id)
    resp = await async_client.post(
        f"{ATT_URL}/{att_id}/confirm-auto-clockout", headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["auto_clock_out_confirmed_at"] is not None
    assert body["auto_clock_out_confirmed_by"] == str(test_users["testadmin"]["id"])
    # anomaly 는 이력으로 유지
    assert "auto_clocked_out" in (body["anomalies"] or [])

    att = await _fetch(att_id)
    assert att.auto_clock_out_confirmed_at is not None
    assert att.auto_clock_out_confirmed_by == test_users["testadmin"]["id"]


async def test_confirm_without_anomaly_400(
    async_client, admin_headers, test_users, test_store_id,
):
    att_id = await _make_auto_clocked_out(test_users, test_store_id, with_anomaly=False)
    resp = await async_client.post(
        f"{ATT_URL}/{att_id}/confirm-auto-clockout", headers=admin_headers,
    )
    assert resp.status_code == 400, resp.text
    assert "not auto clocked out" in resp.json()["detail"]

    att = await _fetch(att_id)
    assert att.auto_clock_out_confirmed_at is None


async def test_confirm_idempotent_noop_preserves_first_confirmation(
    async_client, admin_headers, test_users, test_store_id,
):
    """재확인은 no-op 성공 — 최초 확인 시각/확인자 그대로."""
    att_id = await _make_auto_clocked_out(test_users, test_store_id)
    first = await async_client.post(
        f"{ATT_URL}/{att_id}/confirm-auto-clockout", headers=admin_headers,
    )
    assert first.status_code == 200, first.text
    first_at = first.json()["auto_clock_out_confirmed_at"]

    second = await async_client.post(
        f"{ATT_URL}/{att_id}/confirm-auto-clockout", headers=admin_headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["auto_clock_out_confirmed_at"] == first_at
    assert second.json()["auto_clock_out_confirmed_by"] == str(test_users["testadmin"]["id"])


async def test_correction_of_clock_out_implies_confirm(
    async_client, admin_headers, test_users, test_store_id,
):
    """clock_out 시각 정정 = human-verified → 확인 자동 기록, anomaly 는 유지."""
    att_id = await _make_auto_clocked_out(test_users, test_store_id)
    today = datetime.now(timezone.utc).date()
    resp = await async_client.patch(
        f"{ATT_URL}/{att_id}/correct",
        json={
            "field_name": "clock_out",
            "corrected_value": f"{today.isoformat()}T07:45",
            "reason": "actual leave time verified by manager",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text

    att = await _fetch(att_id)
    assert att.auto_clock_out_confirmed_at is not None
    assert att.auto_clock_out_confirmed_by == test_users["testadmin"]["id"]
    assert "auto_clocked_out" in (att.anomalies or [])


async def test_correction_of_other_field_does_not_confirm(
    async_client, admin_headers, test_users, test_store_id,
):
    """clock_in/note 등 다른 필드 정정은 확인으로 간주하지 않는다."""
    att_id = await _make_auto_clocked_out(test_users, test_store_id)
    today = datetime.now(timezone.utc).date()
    resp = await async_client.patch(
        f"{ATT_URL}/{att_id}/correct",
        json={
            "field_name": "clock_in",
            "corrected_value": f"{today.isoformat()}T00:45",
            "reason": "clock-in fix only",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text

    att = await _fetch(att_id)
    assert att.auto_clock_out_confirmed_at is None


async def test_reopen_then_manual_clock_out_implies_confirm(
    async_client, admin_headers, test_users, test_store_id,
):
    """reopen 후 액션으로 clock-out 재기록해도 확인으로 간주 (corrected == verified)."""
    att_id = await _make_auto_clocked_out(test_users, test_store_id)
    today = datetime.now(timezone.utc).date()

    reopen = await async_client.post(
        f"{ATT_URL}/{att_id}/actions/reopen",
        json={"reason": "undo auto clock-out"}, headers=admin_headers,
    )
    assert reopen.status_code == 200, reopen.text

    clock_out = await async_client.post(
        f"{ATT_URL}/{att_id}/actions/clock-out",
        json={"at": f"{today.isoformat()}T07:30", "reason": "actual time"},
        headers=admin_headers,
    )
    assert clock_out.status_code == 200, clock_out.text
    assert clock_out.json()["auto_clock_out_confirmed_at"] is not None

    att = await _fetch(att_id)
    assert att.auto_clock_out_confirmed_by == test_users["testadmin"]["id"]


async def test_list_and_detail_expose_confirmation_fields(
    async_client, admin_headers, test_users, test_store_id,
):
    """목록/상세 응답에 미확인 배지용 필드가 포함된다 (미확인 = 둘 다 null)."""
    att_id = await _make_auto_clocked_out(test_users, test_store_id)

    listing = await async_client.get(
        ATT_URL,
        params={"store_id": str(test_store_id),
                "work_date": datetime.now(timezone.utc).date().isoformat()},
        headers=admin_headers,
    )
    assert listing.status_code == 200, listing.text
    items = [i for i in listing.json()["items"] if i["id"] == str(att_id)]
    assert items, "생성한 attendance 가 목록에 없음"
    assert items[0]["auto_clock_out_confirmed_at"] is None
    assert items[0]["auto_clock_out_confirmed_by"] is None

    detail = await async_client.get(f"{ATT_URL}/{att_id}", headers=admin_headers)
    assert detail.status_code == 200, detail.text
    assert "auto_clock_out_confirmed_at" in detail.json()
    assert "auto_clock_out_confirmed_by" in detail.json()
