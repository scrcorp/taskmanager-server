"""Integration — payroll_export_service.build_period_export 의 무활동 직원 옵션.

대상: app/services/payroll_export_service.py
    include_idle_members — 기간에 급여 활동이 없는 **재직** 직원을 0 행으로
    덧붙인다. 비활성 계정·퇴직 소속은 어느 경우에도 나오지 않고, 기본(False)은
    로스터 그대로다. open(DRAFT) / confirmed(동결 entries) 두 경로 모두.

픽스처·헬퍼는 calc 골든 테스트와 공유한다 (같은 throwaway store).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select

from app.database import async_session
from app.models.org_member import OrgMember
from app.models.payroll import PayPeriod, PayrollEntry
from app.models.user import User
from app.schemas.payroll import CALC_VERSION, EntryBreakdown
from app.services.payroll_export_service import (
    EXPORT_SHEET_TITLE,
    payroll_export_service,
)
from app.services.payroll_period_service import payroll_period_service
from tests.integration.services.test_payroll_calc_service import (  # noqa: F401
    _JUL_MID,
    _MON,
    _mk_attendance,
    _mk_user,
    calc_ctx,
)


def _names(wb, *, header_row: int) -> list[str]:
    ws = wb[EXPORT_SHEET_TITLE]
    return [
        ws.cell(row=r, column=3).value for r in range(header_row + 1, ws.max_row + 1)
    ]


async def _seed_idle_members(ctx: dict) -> dict:
    """재직 무활동 1 / 비활성 계정 1 / 퇴직 소속 1 — 전부 매장 배정(empid) 있음."""
    active = await _mk_user(ctx, "Idle Active", empid=21)
    deactivated = await _mk_user(ctx, "Idle Deactivated", empid=22)
    terminated = await _mk_user(ctx, "Idle Terminated", empid=23)
    async with async_session() as db:
        user = await db.get(User, deactivated["user_id"])
        user.is_active = False
        user.status = "deactivated"
        member = await db.get(OrgMember, terminated["member_id"])
        member.status = "terminated"
        await db.commit()
    return {"active": active, "deactivated": deactivated, "terminated": terminated}


async def test_open_period_export_default_keeps_roster_only(calc_ctx: dict) -> None:
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=480)
    await _seed_idle_members(calc_ctx)

    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_group_id=calc_ctx["group_id"], date_in_period=_JUL_MID
        )
        result = await payroll_export_service.build_period_export(db, period)
        await db.commit()

    assert result.is_draft is True
    assert _names(result.workbook, header_row=2) == ["Calc Main"]


async def test_open_period_export_appends_active_idle_members_only(
    calc_ctx: dict,
) -> None:
    """재직 무활동만 0 행으로 — 비활성 계정·퇴직 소속은 옵션을 켜도 없다."""
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=480)
    await _seed_idle_members(calc_ctx)

    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_group_id=calc_ctx["group_id"], date_in_period=_JUL_MID
        )
        result = await payroll_export_service.build_period_export(
            db, period, include_idle_members=True
        )
        await db.commit()

    ws = result.workbook[EXPORT_SHEET_TITLE]
    assert _names(result.workbook, header_row=2) == ["Calc Main", "Idle Active"]
    idle_row = ws.max_row
    assert ws.cell(row=idle_row, column=1).value == 21  # empid 스냅샷
    assert ws.cell(row=idle_row, column=4).value == 0  # Regular Hours
    assert ws.cell(row=idle_row, column=13).value == 0  # Gross Pay


async def test_idle_option_does_not_duplicate_members_already_in_roster(
    calc_ctx: dict,
) -> None:
    """로스터에 있는 재직 직원은 idle 로 다시 붙지 않는다."""
    await _mk_attendance(calc_ctx, work_date=_MON, total_work_minutes=480)

    async with async_session() as db:
        period = await payroll_period_service.ensure_period(
            db, store_group_id=calc_ctx["group_id"], date_in_period=_JUL_MID
        )
        result = await payroll_export_service.build_period_export(
            db, period, include_idle_members=True
        )
        await db.commit()

    assert _names(result.workbook, header_row=2).count("Calc Main") == 1


async def test_confirmed_period_export_appends_active_idle_members(
    calc_ctx: dict,
) -> None:
    """동결 entries 경로도 같은 규칙 — entries 에 없는 재직 직원만 0 행."""
    idle = await _seed_idle_members(calc_ctx)
    async with async_session() as db:
        period = PayPeriod(
            organization_id=calc_ctx["org_id"],
            store_group_id=calc_ctx["group_id"],
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 15),
            status="confirmed",
            confirmed_at=datetime.now(timezone.utc),
        )
        db.add(period)
        await db.flush()
        db.add(
            PayrollEntry(
                pay_period_id=period.id,
                organization_id=calc_ctx["org_id"],
                user_id=calc_ctx["user_id"],
                org_member_id=calc_ctx["member_id"],
                member_name="Calc Main",
                calc_version=CALC_VERSION,
                breakdown=EntryBreakdown().model_dump(mode="json"),
                regular_minutes=480,
            )
        )
        await db.commit()
        period_id = period.id

    async with async_session() as db:
        period = await db.scalar(select(PayPeriod).where(PayPeriod.id == period_id))
        plain = await payroll_export_service.build_period_export(db, period)
        with_idle = await payroll_export_service.build_period_export(
            db, period, include_idle_members=True
        )

    assert plain.is_draft is False
    assert _names(plain.workbook, header_row=1) == ["Calc Main"]
    assert _names(with_idle.workbook, header_row=1) == ["Calc Main", "Idle Active"]
    assert idle["active"]["user_id"] != calc_ctx["user_id"]
