"""API integration — 조기 출근 강행 확인 플로우 + payroll 마감 게이트.

검증 대상:
    - POST /console/attendances/{id}/confirm-early-clockin
        happy: confirmed_by/at 기록 + 응답 노출 (anomaly 는 이력으로 유지)
        400: early_clock_in_override anomaly 없는 record
        멱등: 재확인은 no-op 성공 — 최초 확인자/시각 보존
    - 미확인 건이 있으면 payroll 확정이 막힌다 (게이트 ⑥)
    - 확인하면 그 게이트는 사라진다
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from uuid import UUID

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.attendance import Attendance
from app.models.organization import Store
from app.schemas.payroll import VALIDATION_UNCONFIRMED_EARLY_CLOCK_IN
from app.services.attendance_service import ANOMALY_EARLY_CLOCK_IN_OVERRIDE

pytestmark = pytest.mark.asyncio

ATT_URL = "/api/v1/console/attendances"


async def _make_early_clock_in(
    test_users: dict,
    test_store_id: UUID,
    *,
    with_anomaly: bool = True,
    work_date: date | None = None,
) -> UUID:
    """조기 출근 강행 attendance row 생성 (기본: 오늘 UTC)."""
    staff = test_users["teststaff"]
    today = work_date or datetime.now(timezone.utc).date()
    async with async_session() as db:
        att = Attendance(
            organization_id=staff["organization_id"],
            store_id=test_store_id,
            user_id=staff["id"],
            schedule_id=None,
            work_date=today,
            clock_in=datetime.combine(today, time(6, 30), tzinfo=timezone.utc),
            clock_in_timezone="UTC",
            clock_out=datetime.combine(today, time(14, 30), tzinfo=timezone.utc),
            clock_out_timezone="UTC",
            status="clocked_out",
            anomalies=[ANOMALY_EARLY_CLOCK_IN_OVERRIDE] if with_anomaly else None,
            total_work_minutes=480,
        )
        db.add(att)
        await db.commit()
        return att.id


async def _fetch(att_id: UUID) -> Attendance:
    async with async_session() as db:
        return (
            await db.execute(select(Attendance).where(Attendance.id == att_id))
        ).scalar_one()


async def test_confirm_happy_path(
    async_client, admin_headers, test_users, test_store_id
) -> None:
    att_id = await _make_early_clock_in(test_users, test_store_id)

    resp = await async_client.post(
        f"{ATT_URL}/{att_id}/confirm-early-clockin", headers=admin_headers
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["early_clock_in_confirmed_at"] is not None
    assert body["early_clock_in_confirmed_by"] == str(test_users["testadmin"]["id"])
    # anomaly 는 "이런 일이 있었다" 는 이력이라 확인 후에도 남는다.
    assert ANOMALY_EARLY_CLOCK_IN_OVERRIDE in (body["anomalies"] or [])

    att = await _fetch(att_id)
    assert att.early_clock_in_confirmed_by == test_users["testadmin"]["id"]


async def test_confirm_rejects_non_override_record(
    async_client, admin_headers, test_users, test_store_id
) -> None:
    """override 가 아닌 record 는 확인할 게 없다 → 400."""
    att_id = await _make_early_clock_in(test_users, test_store_id, with_anomaly=False)

    resp = await async_client.post(
        f"{ATT_URL}/{att_id}/confirm-early-clockin", headers=admin_headers
    )

    assert resp.status_code == 400, resp.text


async def test_confirm_is_idempotent(
    async_client, admin_headers, test_users, test_store_id
) -> None:
    """재확인은 no-op 성공 — 최초 확인자/시각을 덮어쓰지 않는다(더블클릭 안전)."""
    att_id = await _make_early_clock_in(test_users, test_store_id)

    first = await async_client.post(
        f"{ATT_URL}/{att_id}/confirm-early-clockin", headers=admin_headers
    )
    assert first.status_code == 200, first.text
    first_at = first.json()["early_clock_in_confirmed_at"]

    second = await async_client.post(
        f"{ATT_URL}/{att_id}/confirm-early-clockin", headers=admin_headers
    )
    assert second.status_code == 200, second.text
    assert second.json()["early_clock_in_confirmed_at"] == first_at


async def test_payroll_gate_blocks_until_confirmed(
    async_client, admin_headers, test_users, test_store_id
) -> None:
    """미확인 조기 출근이 있으면 payroll 확정이 막히고, 확인하면 그 게이트가 사라진다.

    급여 확정 전 확인을 강제하는 것이 이 기능의 핵심 안전장치라 게이트까지 직접 본다.
    """
    from sqlalchemy import delete

    from app.models.payroll import PayPeriod
    from app.services.payroll_confirm_service import payroll_confirm_service

    # 다른 테스트가 오늘 날짜 pay_period 를 쓰므로(store+start_date UNIQUE) 이
    # 테스트만의 날짜를 쓴다. 남기면 다음 실행에서 충돌하므로 끝나고 지운다.
    gate_date = date(2031, 3, 3)
    att_id = await _make_early_clock_in(
        test_users, test_store_id, work_date=gate_date
    )
    att = await _fetch(att_id)
    period_id: UUID | None = None

    try:
        async with async_session() as db:
            period = PayPeriod(
                organization_id=att.organization_id,
                store_id=test_store_id,
                start_date=gate_date,
                end_date=gate_date,
                status="open",
            )
            db.add(period)
            await db.commit()
            await db.refresh(period)
            period_id = period.id

            gate_store = await db.get(Store, test_store_id)
            failures = await payroll_confirm_service._evaluate_gates(
                db, period, [gate_store], []
            )
            assert VALIDATION_UNCONFIRMED_EARLY_CLOCK_IN in {f.gate for f in failures}

        # 확인 후 재평가 — 이 게이트만 사라진다.
        resp = await async_client.post(
            f"{ATT_URL}/{att_id}/confirm-early-clockin", headers=admin_headers
        )
        assert resp.status_code == 200, resp.text

        async with async_session() as db:
            period = (
                await db.execute(select(PayPeriod).where(PayPeriod.id == period_id))
            ).scalar_one()
            gate_store = await db.get(Store, test_store_id)
            failures = await payroll_confirm_service._evaluate_gates(
                db, period, [gate_store], []
            )
            assert VALIDATION_UNCONFIRMED_EARLY_CLOCK_IN not in {
                f.gate for f in failures
            }
    finally:
        async with async_session() as db:
            if period_id is not None:
                await db.execute(delete(PayPeriod).where(PayPeriod.id == period_id))
            await db.execute(delete(Attendance).where(Attendance.id == att_id))
            await db.commit()
