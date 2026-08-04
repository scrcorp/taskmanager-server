"""Integration tests — weekly-summary / overtime-alerts C1 net + 매장별 노동법 기준.

P0-1: GET /api/v1/console/attendances/weekly-summary 의 net_work_minutes 가
      attendance 별 C1 net(unpaid 전체 + paid 10분 초과분만 차감)의 합인지 검증.
      (기존 버그: gross - 전체 break — paid 10분 이내도 차감됐음)
P0-2: GET /api/v1/console/attendances/overtime-alerts 가 gross 가 아닌
      C1 net 시간으로 주간 한도를 판정하는지 검증.
P0-3: 초과근무 기준(max weekly)이 매장별 LaborLawSetting 로 resolve 되는지 검증.

주의: 테스트 데이터는 고정 과거 주(2026-06-07 ~ 06-13)에 생성하고,
테스트 유저의 해당 주 attendance 를 전후로 purge 해 dev DB 잔재와 격리한다.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete

from app.database import async_session
from app.models.attendance import Attendance
from app.models.attendance_break import (
    BREAK_TYPE_PAID_10MIN,
    BREAK_TYPE_UNPAID_MEAL,
    AttendanceBreak,
)
from app.models.organization import LaborLawSetting


pytestmark = pytest.mark.asyncio


# 고정 과거 주 — 2026-06-10(수) 포함 주: 일요일 시작 06-07 ~ 토요일 06-13
WEEK_DATE = date(2026, 6, 10)
WEEK_START = date(2026, 6, 7)
WEEK_END = date(2026, 6, 13)


async def _purge_week_attendance(user_ids: list[UUID]) -> None:
    """테스트 유저의 대상 주 attendance 제거 (매장 무관 — dev DB 잔재 격리)."""
    async with async_session() as db:
        await db.execute(
            delete(Attendance).where(
                Attendance.user_id.in_(user_ids),
                Attendance.work_date >= WEEK_START,
                Attendance.work_date <= WEEK_END,
            )
        )
        await db.commit()


async def _delete_labor_settings(store_ids: list[UUID]) -> None:
    async with async_session() as db:
        await db.execute(
            delete(LaborLawSetting).where(LaborLawSetting.store_id.in_(store_ids))
        )
        await db.commit()


@pytest_asyncio.fixture
async def clean_week(test_users: dict, test_store_id: UUID, second_store_id: UUID):
    """대상 주 attendance + 테스트 매장 LaborLawSetting 전후 정리."""
    user_ids = [info["id"] for info in test_users.values()]
    store_ids = [test_store_id, second_store_id]
    await _purge_week_attendance(user_ids)
    await _delete_labor_settings(store_ids)
    yield
    await _purge_week_attendance(user_ids)
    await _delete_labor_settings(store_ids)


async def _create_attendance(
    *,
    org_id: UUID,
    store_id: UUID,
    user_id: UUID,
    work_date: date,
    total_work_minutes: int,
    total_break_minutes: int = 0,
    breaks: list[tuple[str, int]] | None = None,
) -> UUID:
    """clocked_out attendance + break 세션 직접 생성 (과거 데이터 시뮬레이션)."""
    async with async_session() as db:
        att = Attendance(
            organization_id=org_id,
            store_id=store_id,
            user_id=user_id,
            work_date=work_date,
            status="clocked_out",
            total_work_minutes=total_work_minutes,
            total_break_minutes=total_break_minutes,
        )
        db.add(att)
        await db.flush()
        base = datetime.combine(work_date, time(12, 0), tzinfo=timezone.utc)
        offset = 0
        for break_type, duration in breaks or []:
            db.add(
                AttendanceBreak(
                    attendance_id=att.id,
                    started_at=base + timedelta(minutes=offset),
                    ended_at=base + timedelta(minutes=offset + duration),
                    break_type=break_type,
                    duration_minutes=duration,
                )
            )
            offset += duration + 5
        await db.commit()
        return att.id


def _find_row(rows: list[dict], user_id: UUID) -> dict | None:
    return next((r for r in rows if r["user_id"] == str(user_id)), None)


# ── P0-1: weekly-summary — C1 net 합산 ─────────────────────────────


async def test_weekly_summary_uses_c1_net_formula(
    async_client: AsyncClient,
    admin_headers: dict,
    clean_week,
    seed_organization: dict,
    test_users: dict,
    test_store_id: UUID,
) -> None:
    """주간 net = 일별 C1 net 의 합. gross - 전체 break(665) 가 아니라 685."""
    org_id = seed_organization["id"]
    staff = test_users["teststaff"]

    # Day 1: 480분 근무, unpaid 30 + paid 15 → net 480-30-5 = 445
    await _create_attendance(
        org_id=org_id, store_id=test_store_id, user_id=staff["id"],
        work_date=WEEK_START, total_work_minutes=480, total_break_minutes=45,
        breaks=[(BREAK_TYPE_UNPAID_MEAL, 30), (BREAK_TYPE_PAID_10MIN, 15)],
    )
    # Day 2: 240분 근무, paid 10 → net 240 (10분 이내 paid 는 차감 없음)
    await _create_attendance(
        org_id=org_id, store_id=test_store_id, user_id=staff["id"],
        work_date=WEEK_START + timedelta(days=1), total_work_minutes=240,
        total_break_minutes=10, breaks=[(BREAK_TYPE_PAID_10MIN, 10)],
    )

    resp = await async_client.get(
        "/api/v1/console/attendances/weekly-summary",
        headers=admin_headers,
        params={"week_date": str(WEEK_DATE), "user_id": str(staff["id"])},
    )
    assert resp.status_code == 200, resp.text
    row = _find_row(resp.json(), staff["id"])
    assert row is not None
    assert row["week_start"] == str(WEEK_START)
    assert row["week_end"] == str(WEEK_END)
    assert row["days_worked"] == 2
    assert row["total_work_minutes"] == 720
    assert row["total_break_minutes"] == 55
    # C1: (480-30-5) + 240 = 685. 구식 gross-break 공식이면 720-55=665 (버그).
    assert row["net_work_minutes"] == 685
    assert row["net_work_hours"] == round(685 / 60, 1)


async def test_weekly_summary_paid_break_within_10min_not_deducted(
    async_client: AsyncClient,
    admin_headers: dict,
    clean_week,
    seed_organization: dict,
    test_users: dict,
    test_store_id: UUID,
) -> None:
    """paid 10분 break 만 있는 날은 net == total_work (차감 0)."""
    org_id = seed_organization["id"]
    sv = test_users["testsv"]

    await _create_attendance(
        org_id=org_id, store_id=test_store_id, user_id=sv["id"],
        work_date=WEEK_START + timedelta(days=2), total_work_minutes=300,
        total_break_minutes=10, breaks=[(BREAK_TYPE_PAID_10MIN, 10)],
    )

    resp = await async_client.get(
        "/api/v1/console/attendances/weekly-summary",
        headers=admin_headers,
        params={"week_date": str(WEEK_DATE), "user_id": str(sv["id"])},
    )
    assert resp.status_code == 200, resp.text
    row = _find_row(resp.json(), sv["id"])
    assert row is not None
    assert row["net_work_minutes"] == 300
    assert row["total_break_minutes"] == 10


# ── P0-2: overtime-alerts — gross 아닌 C1 net 으로 판정 ────────────


async def test_overtime_alerts_use_net_not_gross(
    async_client: AsyncClient,
    admin_headers: dict,
    clean_week,
    seed_organization: dict,
    test_users: dict,
    test_store_id: UUID,
) -> None:
    """gross 41h / net 39h → 미플래그. net 41h → 플래그 (기본 한도 40h)."""
    org_id = seed_organization["id"]
    sv = test_users["testsv"]      # gross 41h, net 39h — 플래그되면 안 됨
    gm = test_users["testgm"]      # net 41h — 플래그되어야 함

    for i in range(6):
        # sv: 일 410분 근무 + unpaid 20 → net 390 → 주간 2340분 = 39h (gross 2460 = 41h)
        await _create_attendance(
            org_id=org_id, store_id=test_store_id, user_id=sv["id"],
            work_date=WEEK_START + timedelta(days=i), total_work_minutes=410,
            total_break_minutes=20, breaks=[(BREAK_TYPE_UNPAID_MEAL, 20)],
        )
        # gm: 일 410분 근무, break 없음 → 주간 2460분 = 41h net
        await _create_attendance(
            org_id=org_id, store_id=test_store_id, user_id=gm["id"],
            work_date=WEEK_START + timedelta(days=i), total_work_minutes=410,
        )

    resp = await async_client.get(
        "/api/v1/console/attendances/overtime-alerts",
        headers=admin_headers,
        params={"week_date": str(WEEK_DATE), "store_id": str(test_store_id)},
    )
    assert resp.status_code == 200, resp.text
    alerts = resp.json()

    # gross 41h 이지만 net 39h — 구식 gross 판정이었다면 잘못 플래그됐을 케이스
    assert _find_row(alerts, sv["id"]) is None

    gm_alert = _find_row(alerts, gm["id"])
    assert gm_alert is not None
    assert gm_alert["total_hours"] == 41.0
    assert gm_alert["max_weekly_hours"] == 40
    assert gm_alert["overtime_hours"] == 1.0


# ── P0-3: overtime-alerts — 매장별 LaborLawSetting 기준 ────────────


async def test_overtime_alerts_per_store_threshold(
    async_client: AsyncClient,
    admin_headers: dict,
    clean_week,
    seed_organization: dict,
    test_users: dict,
    test_store_id: UUID,
    second_store_id: UUID,
) -> None:
    """매장 A(한도 30h)의 35h → 플래그, 매장 B(한도 45h)의 41h → 미플래그."""
    org_id = seed_organization["id"]
    staff = test_users["teststaff"]  # 매장 A 근무 35h net
    sv = test_users["testsv"]        # 매장 B 근무 41h net

    async with async_session() as db:
        db.add(LaborLawSetting(
            organization_id=org_id, store_id=test_store_id,
            federal_max_weekly=40, state_max_weekly=None, store_max_weekly=30,
        ))
        db.add(LaborLawSetting(
            organization_id=org_id, store_id=second_store_id,
            federal_max_weekly=40, state_max_weekly=None, store_max_weekly=45,
        ))
        await db.commit()

    for i in range(5):
        # staff @ 매장 A: 일 420분 → 주간 2100분 = 35h
        await _create_attendance(
            org_id=org_id, store_id=test_store_id, user_id=staff["id"],
            work_date=WEEK_START + timedelta(days=i), total_work_minutes=420,
        )
    for i in range(6):
        # sv @ 매장 B: 일 410분 → 주간 2460분 = 41h
        await _create_attendance(
            org_id=org_id, store_id=second_store_id, user_id=sv["id"],
            work_date=WEEK_START + timedelta(days=i), total_work_minutes=410,
        )

    resp = await async_client.get(
        "/api/v1/console/attendances/overtime-alerts",
        headers=admin_headers,
        params={"week_date": str(WEEK_DATE)},
    )
    assert resp.status_code == 200, resp.text
    alerts = resp.json()

    # 매장 A 한도 30h < 35h → 플래그, 기준은 A 매장 설정
    staff_alert = _find_row(alerts, staff["id"])
    assert staff_alert is not None
    assert staff_alert["max_weekly_hours"] == 30
    assert staff_alert["total_hours"] == 35.0
    assert staff_alert["overtime_hours"] == 5.0

    # 매장 B 한도 45h > 41h → 미플래그 (임의 row 적용 버그였다면 A 의 30h 로 잘못 플래그)
    assert _find_row(alerts, sv["id"]) is None


async def test_overtime_alerts_default_40_when_no_setting(
    async_client: AsyncClient,
    admin_headers: dict,
    clean_week,
    seed_organization: dict,
    test_users: dict,
    test_store_id: UUID,
) -> None:
    """LaborLawSetting row 없는 매장은 기본 40h 기준."""
    org_id = seed_organization["id"]
    gm = test_users["testgm"]

    for i in range(6):
        await _create_attendance(
            org_id=org_id, store_id=test_store_id, user_id=gm["id"],
            work_date=WEEK_START + timedelta(days=i), total_work_minutes=420,
        )  # 주간 2520분 = 42h

    resp = await async_client.get(
        "/api/v1/console/attendances/overtime-alerts",
        headers=admin_headers,
        params={"week_date": str(WEEK_DATE), "store_id": str(test_store_id)},
    )
    assert resp.status_code == 200, resp.text
    alert = _find_row(resp.json(), gm["id"])
    assert alert is not None
    assert alert["max_weekly_hours"] == 40
    assert alert["overtime_hours"] == 2.0
