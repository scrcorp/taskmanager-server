"""Payroll Confirm Service — L5 마감 게이트 + 기간 확정/동결 (Payroll v1 Phase 3).

pay period 'open' → 'confirmed' 전이의 유일한 경로. 스펙: docs/99_inbox/2026-08-03
payroll-v1-스키마-스펙.md §4/§5 + 설계방향 L1~L6, 계산 규칙 1~5.

마감 게이트 (L5 — 전부 평가해서 한 번에 보고, 하나라도 걸리면 409):
    ① 미확인 자동퇴근 0건 — anomaly 'auto_clocked_out' 이면서
       auto_clock_out_confirmed_at IS NULL 인 attendance (L6 확인 플로우가 해소)
    ② 열린 근무 0건 — clock_in 有 + clock_out 無 (non-cancelled)
    ③ rate 게이트 — preview validation 의 rate_missing / below_minimum_wage 0건
       (판정은 rate_at 기준 — 계산 규칙 5, C8)
    ④ 대응 tip_period(같은 store+범위) status == 'confirmed' (계산 규칙 4)
    ⑤ 멀티스토어 주간 정합 (계산 규칙 2) — 아래 참조
    ⑦ 겹친 근무 0건 — 같은 사람의 두 근무가 시간상 겹치면 같은 시간이 두 번 지급된다.
       anomaly 가 아니라 **시간 구간**으로 판정하고(콘솔 경로는 라벨을 안 붙인다),
       org 전역 · **닫힌 건만**(열린 건은 지급되지 않으므로 ② 가 담당) ·
       짝 찾기 창은 기간보다 넓다(경계를 걸친 쌍) · 확인 도장 없음(정정/취소만이 해소).
    ⑥ 미확인 조기 출근 강행 0건 — anomaly 'early_clock_in_override' 이면서
       early_clock_in_confirmed_at IS NULL 인 attendance. 실제 clock-in 시각을
       그대로 인정하므로 예정 밖 시간이 급여에 들어간다 — 확정 전 사람이 본다.
       (매니저 대행 건은 생성 시점에 확인 처리되어 걸리지 않음)

동결 (단일 트랜잭션 — 전부 성공 또는 전부 없던 일):
    preview_period 재실행 → 행별 breakdown 합계 == 스칼라 검증(불일치 시 중단)
    → payroll_entries INSERT (revision 0, 스냅샷: member_name=display_name 경유
    preview 값, empid=이 store 의 org_member_stores, crewid=org_members)
    → 범위 내 유효(non-voided) payroll_events 에 pay_period_id 스탬프(동결)
    → period confirmed_at/by 기록. commit 은 이 서비스가 소유 (단독 API 액션).

unconfirm 없음 (v1):
    확정 해제 API 를 두지 않는다 — 확정 후 정정은 amendment(Q3, 외부 확인 대기)
    경로로만. revision 컬럼이 그 탈출구다 (v1 은 항상 0).

멀티스토어 커플링 (계산 규칙 2) — v1 단순화와 한계:
    같은 org 사용자가 겹치는 주(Sun–Sat)에 다른 매장에서도 근무한 경우, 주 40h /
    7일 연속 / 일 8h 판정은 org 합산이 법적 기준이지만 v1 계산 엔진은 매장 단위다.
    confirm 시 org 합산 정합 체크를 돌려, 매장 단위 계산이 과소지급이 되는 조합
    (합산 주 40h 초과 / org 관점 7일 연속 / 같은 날 두 매장 합산 8h 초과)이
    감지되면 설명과 함께 거부한다 — v1 은 cross-store premium 자동 분배를 하지
    않는다 (수동 처리 유도).
    한계 (문서화):
    - 타 매장 시간은 live attendance 로 읽는다. 선확정된 형제 기간은 L3 lock 이
      attendance 수정을 막으므로 frozen entries 와 live 가 일치한다는 전제
      (frozen breakdown 재파싱 대신 단순화). lock enforcement 이전 데이터는 예외.
    - 타 매장의 열린 근무(clock_out 無)는 net 미계산이라 합산에서 빠진다 —
      그 매장의 자체 confirm 게이트 ② 가 잡는다.
    - 감지는 이 매장 preview 행에 있는 사용자만 대상 (타 매장 전용 근무자는
      그 매장 confirm 몫).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.payroll_rules import (
    CA_DAILY_OT_HOURS,
    MINIMUM_WAGE,
    WEEKLY_OT_HOURS,
)
from app.models.attendance import Attendance
from app.models.org_member import OrgMember
from app.models.payroll import PayPeriod, PayrollEntry, PayrollEvent
from app.models.user import User
from app.schemas.payroll import (
    CODE_ALREADY_CONFIRMED,
    CODE_CLOSE_GATES_FAILED,
    GATE_MULTI_STORE_WEEK,
    VALIDATION_BELOW_MINIMUM_WAGE,
    VALIDATION_OPEN_SHIFT,
    VALIDATION_OVERLAPPING_ATTENDANCE,
    VALIDATION_RATE_MISSING,
    VALIDATION_TIP_PERIOD_NOT_CONFIRMED,
    VALIDATION_UNCONFIRMED_AUTO_CLOCKOUT,
    VALIDATION_UNCONFIRMED_EARLY_CLOCK_IN,
    ConfirmGateFailure,
    ConfirmGateItem,
    PayrollPreviewRow,
)
from app.services.attendance_service import ANOMALY_EARLY_CLOCK_IN_OVERRIDE
from app.services.payroll_calc_service import payroll_calc_service
from app.services.payroll_period_service import (
    payroll_period_service,
    week_start_for,
    workweeks_touching,
)
from app.utils.exceptions import NotFoundError
from app.utils.names import display_name

# 자동퇴근 anomaly 코드 (attendance_cron_service 가 기록 — calc 서비스와 동일 값)
_ANOMALY_AUTO_CLOCKED_OUT = "auto_clocked_out"
# 조기 출근 강행 anomaly 코드 (attendance_device_service 가 기록)
_ANOMALY_EARLY_CLOCK_IN_OVERRIDE = ANOMALY_EARLY_CLOCK_IN_OVERRIDE

# 겹침 게이트(⑦) 가 짝을 찾을 때 기간 앞뒤로 더 읽는 일수.
#
# work_date 는 "영업일 라벨" 이고 실제 clock_in/out 은 그 라벨에서 하루 안쪽으로
# 벗어난다(야간조·D4 어제 shift). 그래서 겹치는 두 row 의 work_date 가 하루 차이로
# 갈릴 수 있고, 그 하루가 급여 기간 경계면 양쪽 기간 모두 짝을 못 본다.
# 2일이면 "라벨 하루 + 근무 하루" 를 모두 덮는다 — 창을 넓히는 비용은 몇 row 뿐이고,
# 못 보는 비용은 그 시간만큼의 이중 지급이다.
_OVERLAP_SCAN_MARGIN_DAYS = 2

_DAILY_OT_MIN = CA_DAILY_OT_HOURS * 60  # 480
_WEEKLY_OT_MIN = WEEKLY_OT_HOURS * 60  # 2400

# 멀티스토어 주간 정합 위반 종류 (evaluate_org_week_minutes 반환 kind)
ORG_WEEK_OVER_40H = "weekly_over_40h"
ORG_WEEK_SEVENTH_DAY_SPLIT = "seventh_day_split"
ORG_WEEK_DAILY_OVER_8H = "daily_over_8h"


def _fmt_hours(minutes: int) -> str:
    """분 → 'H.T' 표기 (사람이 읽는 메시지용). 2700 → '45.0'."""
    return f"{minutes / 60:.1f}"


# ---------------------------------------------------------------------------
# 순수 규칙 — DB 없음 (unit test 대상)
# ---------------------------------------------------------------------------


def verify_row_consistency(row: PayrollPreviewRow) -> list[str]:
    """breakdown 합계 == 스칼라 필드 검증 (스펙 §5 confirm 시 일치 검증).

    라운딩 규칙(구간별 센트 반올림 1회 후 정확한 합) 덕에 등식이 정확히
    성립해야 한다 — 불일치는 계산 엔진 버그이며 동결하면 안 된다.

    Returns:
        발견된 불일치 설명 목록 (빈 목록 = 일관성 OK)
    """
    problems: list[str] = []
    bd = row.breakdown

    day_reg = sum(d.regular_minutes for d in bd.days)
    day_ot = sum(d.ot_minutes for d in bd.days)
    day_dt = sum(d.dt_minutes for d in bd.days)
    if (day_reg, day_ot, day_dt) != (
        row.regular_minutes, row.ot_minutes, row.dt_minutes
    ):
        problems.append(
            f"day minutes ({day_reg}/{day_ot}/{day_dt}) != scalar minutes "
            f"({row.regular_minutes}/{row.ot_minutes}/{row.dt_minutes})"
        )

    seg_reg = sum(s.regular_minutes for s in bd.segments)
    seg_ot = sum(s.ot_minutes for s in bd.segments)
    seg_dt = sum(s.dt_minutes for s in bd.segments)
    if (seg_reg, seg_ot, seg_dt) != (
        row.regular_minutes, row.ot_minutes, row.dt_minutes
    ):
        problems.append(
            f"segment minutes ({seg_reg}/{seg_ot}/{seg_dt}) != scalar minutes "
            f"({row.regular_minutes}/{row.ot_minutes}/{row.dt_minutes})"
        )

    seg_amount = sum((s.amount for s in bd.segments), start=Decimal("0"))
    work_pay = row.regular_pay + row.ot_pay + row.dt_pay
    if seg_amount != work_pay:
        problems.append(
            f"segment amount total {seg_amount} != regular+ot+dt pay {work_pay}"
        )

    penalty_total = sum((p.amount for p in bd.penalties), start=Decimal("0"))
    if penalty_total != row.penalty_pay:
        problems.append(
            f"penalty line total {penalty_total} != penalty_pay {row.penalty_pay}"
        )

    # 보너스 — 매장별 라인(bonus_lines)이 있으면 그 합이 원천 (group 스코프).
    # 라인이 없는 옛 동결본은 가산율 × 총시간 공식으로 검증한다 (하위호환).
    if bd.bonus_lines:
        expected_bonus = sum(
            (line.amount for line in bd.bonus_lines), start=Decimal("0.00")
        )
        line_mismatch = [
            line
            for line in bd.bonus_lines
            if (line.rate * Decimal(line.minutes) / Decimal(60)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            != line.amount
        ]
        if line_mismatch:
            problems.append(
                f"{len(line_mismatch)} bonus line(s) fail rate x minutes = amount"
            )
        if expected_bonus != row.bonus_pay:
            problems.append(
                f"bonus line total {expected_bonus} != bonus_pay {row.bonus_pay}"
            )
    else:
        total_minutes = row.regular_minutes + row.ot_minutes + row.dt_minutes
        expected_bonus = (
            (bd.bonus_rate * Decimal(total_minutes) / Decimal(60)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if bd.bonus_rate
            else Decimal("0.00")
        )
        if expected_bonus != row.bonus_pay:
            problems.append(
                f"bonus_rate {bd.bonus_rate} x {total_minutes}min = {expected_bonus} "
                f"!= bonus_pay {row.bonus_pay}"
            )

    gross = work_pay + row.penalty_pay + row.bonus_pay + row.card_tips
    if gross != row.gross_pay:
        problems.append(
            f"component sum {gross} != gross_pay {row.gross_pay}"
        )
    return problems


def evaluate_org_week_minutes(
    minutes_by_store: dict[UUID, dict[date, int]],
) -> list[tuple[str, str]]:
    """한 (user, Sun–Sat 주) 의 매장별 net 분 → org 합산 정합 위반 목록.

    매장 1곳뿐이면 항상 빈 목록 — 매장 단위 엔진이 그대로 정확하다.
    2곳 이상일 때만 매장 단위 계산이 놓치는 조합을 찾는다 (계산 규칙 2):
        - weekly_over_40h: 합산 straight 근무가 40h 초과 (주간 OT 분배 불가)
        - seventh_day_split: org 관점 7일 연속인데 어느 한 매장도 7일 전부는 아님
        - daily_over_8h: 같은 날 두 매장 합산이 8h 초과 (일별 OT 분배 불가)

    Args:
        minutes_by_store: {store_id: {work_date: net_minutes}}

    Returns:
        [(kind, human_detail)] — kind 는 ORG_WEEK_* 상수, detail 은 영어 문구
    """
    store_totals = {
        sid: sum(day_map.values()) for sid, day_map in minutes_by_store.items()
    }
    active = {sid: total for sid, total in store_totals.items() if total > 0}
    if len(active) < 2:
        return []

    violations: list[tuple[str, str]] = []
    total = sum(active.values())
    if total > _WEEKLY_OT_MIN:
        violations.append(
            (
                ORG_WEEK_OVER_40H,
                f"combined {_fmt_hours(total)}h across {len(active)} stores "
                f"exceeds {WEEKLY_OT_HOURS}h/week — weekly overtime cannot be "
                "attributed to a single store",
            )
        )

    worked_days = {
        d
        for day_map in minutes_by_store.values()
        for d, minutes in day_map.items()
        if minutes > 0
    }
    if len(worked_days) == 7 and not any(
        len({d for d, m in day_map.items() if m > 0}) == 7
        for day_map in minutes_by_store.values()
    ):
        violations.append(
            (
                ORG_WEEK_SEVENTH_DAY_SPLIT,
                "worked all 7 days of the workweek across multiple stores — "
                "the 7th-consecutive-day premium cannot be computed per store",
            )
        )

    day_totals: dict[date, dict[UUID, int]] = {}
    for sid, day_map in minutes_by_store.items():
        for d, minutes in day_map.items():
            if minutes > 0:
                day_totals.setdefault(d, {})[sid] = minutes
    for d in sorted(day_totals):
        per_store = day_totals[d]
        combined = sum(per_store.values())
        if len(per_store) >= 2 and combined > _DAILY_OT_MIN:
            violations.append(
                (
                    ORG_WEEK_DAILY_OVER_8H,
                    f"worked at {len(per_store)} stores on {d} for a combined "
                    f"{_fmt_hours(combined)}h — daily overtime over "
                    f"{CA_DAILY_OT_HOURS}h cannot be attributed per store",
                )
            )
    return violations


# ---------------------------------------------------------------------------
# 서비스
# ---------------------------------------------------------------------------


@dataclass
class ConfirmResult:
    """confirm_period 성공 결과."""

    period: PayPeriod
    entries: list[PayrollEntry]
    events_frozen: int  # pay_period_id 스탬프된 이벤트 수


class PayrollConfirmService:
    """마감 게이트 평가 + 동결 실행 — 'open' → 'confirmed' 단일 경로."""

    async def confirm_period(
        self,
        db: AsyncSession,
        *,
        period_id: UUID,
        actor: User,
    ) -> ConfirmResult:
        """L5 게이트 전부 통과 시 기간을 확정하고 entries/events 를 동결한다.

        모든 게이트를 평가해 실패를 한 번에 보고한다 (게이트 하나 고칠 때마다
        다음 게이트를 새로 발견하는 waterfall 방지). 동결은 단일 트랜잭션 —
        commit 은 이 함수가 소유한다 (단독 API 액션 관례, tag_event 동일).

        Raises:
            NotFoundError: 기간이 없거나 store 소속이 아닐 때 (404)
            HTTPException 409: 이미 confirmed (detail.code =
                pay_period_already_confirmed) / 게이트 실패 (detail.code =
                payroll_close_gates_failed, detail.gates = 게이트별 상세)
            HTTPException 500: breakdown 합계 != 스칼라 (엔진 버그 — 동결 중단)
        """
        period = await db.get(PayPeriod, period_id)
        if period is None:
            raise NotFoundError("Pay period not found")
        if period.store_group_id is None:
            # 레거시 store 스코프 기간은 전부 확정 상태로만 남아 있다 (전환
            # 마이그레이션이 open 행을 삭제) — 새로 확정할 일이 없다.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": CODE_ALREADY_CONFIRMED,
                    "message": (
                        "This pay period predates the group-scope payroll "
                        "transition and can no longer be confirmed."
                    ),
                },
            )
        if period.status == "confirmed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": CODE_ALREADY_CONFIRMED,
                    "message": (
                        "This pay period is already confirmed. Confirmed payroll "
                        "cannot be re-confirmed or unconfirmed in v1 — "
                        "corrections will be handled through amendments."
                    ),
                },
            )

        # 계산 (이벤트 upsert 는 flush 만 — 게이트 실패 시 rollback 으로 소거)
        rows = await payroll_calc_service.preview_period(db, period)

        stores = await payroll_period_service.group_stores(
            db, period.store_group_id
        )
        gates = await self._evaluate_gates(db, period, stores, rows)
        if gates:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": CODE_CLOSE_GATES_FAILED,
                    "message": (
                        f"Cannot confirm this pay period — {len(gates)} close "
                        "gate(s) failed. Resolve every issue below, then "
                        "confirm again."
                    ),
                    "gates": [g.model_dump(mode="json") for g in gates],
                },
            )

        entries = await self._freeze_entries(db, period, rows)
        events_frozen = await self._stamp_events(
            db, period, [s.id for s in stores]
        )
        period.status = "confirmed"
        period.confirmed_at = datetime.now(timezone.utc)
        period.confirmed_by = actor.id
        try:
            await db.commit()
        except IntegrityError:
            # 동시 confirm 경합 — entry unique(uq_payroll_entry_period_user_rev)
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": CODE_ALREADY_CONFIRMED,
                    "message": (
                        "This pay period was confirmed by another request while "
                        "this one was running. Reload the period to see the "
                        "frozen entries."
                    ),
                },
            )
        return ConfirmResult(period=period, entries=entries, events_frozen=events_frozen)

    # ── 게이트 평가 ──────────────────────────────────────────────

    async def _evaluate_gates(
        self,
        db: AsyncSession,
        period: PayPeriod,
        stores: list,
        rows: list[PayrollPreviewRow],
    ) -> list[ConfirmGateFailure]:
        """L5 게이트 ①~④ + 멀티스토어 정합(계산 규칙 2)을 전부 평가.

        스캔 스코프 = 그룹 내 전 매장 (D2). stores 는 그룹 소속 Store 목록.
        """
        store_ids = [s.id for s in stores]
        failures: list[ConfirmGateFailure] = []
        names: dict[UUID, str] = {r.user_id: r.member_name for r in rows}

        # ① 미확인 자동퇴근 — anomaly + confirmed_at IS NULL (정밀 판정은 여기)
        auto_map = await self._auto_clockout_dates(db, period, store_ids)
        # ② 열린 근무
        open_map = await self._open_shift_dates(db, period, store_ids)
        # ⑥ 미확인 조기 출근 강행
        early_map = await self._early_clock_in_dates(db, period, store_ids)
        # ⑦ 겹친 근무 (D15) — 이중 지급의 최후 방어선
        overlap_map = await self._overlapping_attendance_dates(
            db, period, store_ids
        )
        missing = (
            set(auto_map) | set(open_map) | set(early_map) | set(overlap_map)
        ) - set(names)
        if missing:
            names.update(await self._load_names(db, missing))

        if auto_map:
            failures.append(
                ConfirmGateFailure(
                    gate=VALIDATION_UNCONFIRMED_AUTO_CLOCKOUT,
                    message=(
                        "Auto clock-outs in this period have not been confirmed. "
                        "Open Attendance, review each day and confirm (or "
                        "correct) the auto clock-out time."
                    ),
                    items=[
                        ConfirmGateItem(
                            user_id=user_id,
                            member_name=names.get(user_id),
                            dates=dates,
                            message=(
                                "Unconfirmed auto clock-out on: "
                                + ", ".join(str(d) for d in dates)
                            ),
                        )
                        for user_id, dates in sorted(
                            auto_map.items(), key=lambda kv: str(kv[0])
                        )
                    ],
                )
            )
        if open_map:
            failures.append(
                ConfirmGateFailure(
                    gate=VALIDATION_OPEN_SHIFT,
                    message=(
                        "Open shifts without a clock-out remain in this period. "
                        "Close each shift with the real clock-out time (or "
                        "cancel the record) in Attendance."
                    ),
                    items=[
                        ConfirmGateItem(
                            user_id=user_id,
                            member_name=names.get(user_id),
                            dates=dates,
                            message=(
                                "Open shift without clock-out on: "
                                + ", ".join(str(d) for d in dates)
                            ),
                        )
                        for user_id, dates in sorted(
                            open_map.items(), key=lambda kv: str(kv[0])
                        )
                    ],
                )
            )

        if early_map:
            failures.append(
                ConfirmGateFailure(
                    gate=VALIDATION_UNCONFIRMED_EARLY_CLOCK_IN,
                    message=(
                        "Early clock-ins in this period have not been reviewed. "
                        "Staff clocked in before their shift started, so the extra "
                        "time is included in this payroll. Open Attendance, check "
                        "each one and confirm (or correct) the clock-in time."
                    ),
                    items=[
                        ConfirmGateItem(
                            user_id=user_id,
                            member_name=names.get(user_id),
                            dates=dates,
                            message=(
                                "Unreviewed early clock-in on: "
                                + ", ".join(str(d) for d in dates)
                            ),
                        )
                        for user_id, dates in sorted(
                            early_map.items(), key=lambda kv: str(kv[0])
                        )
                    ],
                )
            )

        if overlap_map:
            failures.append(
                ConfirmGateFailure(
                    gate=VALIDATION_OVERLAPPING_ATTENDANCE,
                    message=(
                        "Two shifts overlap in time for the same person, so the "
                        "same hours would be paid twice. Cancel or correct one of "
                        "them in Attendance, then confirm again."
                    ),
                    items=[
                        ConfirmGateItem(
                            user_id=user_id,
                            member_name=names.get(user_id),
                            dates=dates,
                            message=(
                                "Overlapping shifts on: "
                                + ", ".join(str(d) for d in dates)
                            ),
                        )
                        for user_id, dates in sorted(
                            overlap_map.items(), key=lambda kv: str(kv[0])
                        )
                    ],
                )
            )

        # ③ rate 게이트 — preview validation 재사용 (판정 fork 금지)
        rate_items: list[ConfirmGateItem] = []
        below_items: list[ConfirmGateItem] = []
        for row in rows:
            for validation in row.validations:
                if validation.code == VALIDATION_RATE_MISSING:
                    rate_items.append(
                        ConfirmGateItem(
                            user_id=row.user_id,
                            member_name=row.member_name,
                            dates=[
                                d.work_date
                                for d in row.breakdown.days
                                if d.applied_rate is None or d.applied_rate <= 0
                            ],
                            message=validation.message,
                        )
                    )
                elif validation.code == VALIDATION_BELOW_MINIMUM_WAGE:
                    below_items.append(
                        ConfirmGateItem(
                            user_id=row.user_id,
                            member_name=row.member_name,
                            dates=[
                                d.work_date
                                for d in row.breakdown.days
                                if d.applied_rate is not None
                                and 0 < d.applied_rate < MINIMUM_WAGE
                            ],
                            message=validation.message,
                        )
                    )
        if rate_items:
            failures.append(
                ConfirmGateFailure(
                    gate=VALIDATION_RATE_MISSING,
                    message=(
                        "Some members have no hourly rate (or rate 0) on worked "
                        "days. Set their rate with a rate change effective on or "
                        "before those dates, then confirm again."
                    ),
                    items=rate_items,
                )
            )
        if below_items:
            failures.append(
                ConfirmGateFailure(
                    gate=VALIDATION_BELOW_MINIMUM_WAGE,
                    message=(
                        f"Some applied hourly rates are below the legal minimum "
                        f"wage (${MINIMUM_WAGE}). Raise the member's rate to at "
                        "least the minimum, then confirm again."
                    ),
                    items=below_items,
                )
            )

        # ④ tip period confirmed (계산 규칙 4) — 그룹 내 전 매장
        tip_statuses = await payroll_period_service.tip_period_status_for(
            db, store_ids=store_ids, period=period
        )
        store_names = {s.id: s.name for s in stores}
        pending = {
            sid: tp_status
            for sid, tp_status in tip_statuses.items()
            if tp_status != "confirmed"
        }
        if pending:
            detail = ", ".join(
                f"{store_names.get(sid, str(sid))} ("
                + (tp_status if tp_status is not None else "not created yet")
                + ")"
                for sid, tp_status in sorted(
                    pending.items(), key=lambda kv: store_names.get(kv[0], "")
                )
            )
            failures.append(
                ConfirmGateFailure(
                    gate=VALIDATION_TIP_PERIOD_NOT_CONFIRMED,
                    message=(
                        f"The tip period for {period.start_date} – "
                        f"{period.end_date} is not confirmed at every store in "
                        f"this group: {detail}. Confirm each store's tip period "
                        "in Tips first so card tips are final before they are "
                        "frozen into paychecks."
                    ),
                    items=[],
                )
            )

        # ⑤ 멀티스토어 주간 정합 (계산 규칙 2) — 그룹 밖(cross-group) 겸업만.
        # 그룹 안 겸업은 이제 계산 엔진이 법인 합산으로 직접 계산한다 (D2).
        multi_items = await self._multi_store_items(
            db, period, store_ids, rows, names
        )
        if multi_items:
            failures.append(
                ConfirmGateFailure(
                    gate=GATE_MULTI_STORE_WEEK,
                    message=(
                        "Some members also worked at another store in an "
                        "overlapping workweek, and the combined hours change "
                        "their overtime. v1 cannot split cross-store premiums "
                        "automatically — adjust the records or handle these "
                        "members' pay manually, then confirm again."
                    ),
                    items=multi_items,
                )
            )
        return failures

    async def _auto_clockout_dates(
        self, db: AsyncSession, period: PayPeriod, store_ids: list[UUID]
    ) -> dict[UUID, list[date]]:
        """게이트 ① — 미확인 자동퇴근 (user → 날짜 목록, 정렬)."""
        result = await db.execute(
            select(Attendance.user_id, Attendance.work_date)
            .where(
                Attendance.store_id.in_(store_ids),
                Attendance.work_date >= period.start_date,
                Attendance.work_date <= period.end_date,
                Attendance.status != "cancelled",
                Attendance.user_id.is_not(None),
                Attendance.anomalies.contains([_ANOMALY_AUTO_CLOCKED_OUT]),
                Attendance.auto_clock_out_confirmed_at.is_(None),
            )
            .distinct()
        )
        return self._group_dates(result.all())

    async def _early_clock_in_dates(
        self, db: AsyncSession, period: PayPeriod, store_ids: list[UUID]
    ) -> dict[UUID, list[date]]:
        """게이트 ⑥ — 미확인 조기 출근 강행 (user → 날짜 목록, 정렬).

        실제 clock-in 시각을 그대로 인정하므로 예정 밖 시간이 급여에 들어간다.
        확정 전에 매니저가 한 번 확인하게 만드는 게 이 게이트의 목적.
        매니저 대행 건은 생성 시점에 확인된 상태라 여기 걸리지 않는다.
        """
        result = await db.execute(
            select(Attendance.user_id, Attendance.work_date)
            .where(
                Attendance.store_id.in_(store_ids),
                Attendance.work_date >= period.start_date,
                Attendance.work_date <= period.end_date,
                Attendance.status != "cancelled",
                Attendance.user_id.is_not(None),
                Attendance.anomalies.contains([_ANOMALY_EARLY_CLOCK_IN_OVERRIDE]),
                Attendance.early_clock_in_confirmed_at.is_(None),
            )
            .distinct()
        )
        return self._group_dates(result.all())

    async def _overlapping_attendance_dates(
        self, db: AsyncSession, period: PayPeriod, store_ids: list[UUID]
    ) -> dict[UUID, list[date]]:
        """게이트 ⑦ — 시간이 겹친 근무 (user → 날짜 목록, 정렬).

        **anomaly 플래그가 아니라 시간 구간으로 판정한다.** 콘솔 clock-in 경로에는
        겹침 가드가 아예 없어 라벨 없이 겹침이 만들어질 수 있고, 플래그 기반이면
        그 경로에서 생긴 겹침을 못 잡는다. 판정 규칙 자체는
        `app/utils/attendance_overlap.py` 하나뿐이다(경로마다 다른 답이 나오지 않게).

        - **둘 다 닫힌 건만 본다** — 아래 "열린 근무" 참조.
        - 스코프는 **org 전역**(매장 아님). 두 매장에서 겹쳐 찍은 시간도 똑같이
          두 번 지급된다. 대상자는 이 매장 기간에 근무 기록이 있는 사람으로 한정한다
          (다른 매장 전용 근무자는 그 매장 confirm 몫 — `_multi_store_items` 와 동일 원칙).
        - 확인 도장(escape hatch)이 없다. 해소는 정정/취소뿐이다.

        **기간 경계를 걸친 짝** — 짝 찾기 창은 기간보다 넓다
        (`_OVERLAP_SCAN_MARGIN_DAYS` 만큼 앞뒤로). 다른 게이트처럼 `work_date
        BETWEEN start AND end` 로만 훑으면, 겹치는 두 row 의 work_date 가 기간
        경계를 사이에 두고 갈릴 때 **어느 기간의 게이트도 그 쌍을 보지 못한다**.
        D4(어제 영업일 shift 로 clock-in)와 야간조 겹침이 정확히 (어제, 오늘) 쌍을
        만들기 때문에 반월 급여의 15/16일 경계에서 그대로 성립했다. 이 게이트는
        이중 지급의 최후 방어선이라 '알려진 한계' 로 남기면 돈이 샌다.
        **보고(=차단)는 기간 안의 row 만** 한다 — 기간 밖 row 로 이 기간을 막으면
        해소할 화면이 없고, 그 row 가 속한 기간은 자기 confirm 때 같은 규칙으로 막힌다.

        **열린 근무(clock_out IS NULL)는 대상이 아니다.** 열린 row 는
        `compute_net_work_minutes` 가 None 을 돌려 **애초에 지급되지 않으므로**
        여기서 막아 봐야 이중 지급을 막는 게 아니라 확정만 막는다. 게다가 이
        게이트는 org 전역이라 **다른 매장의 잊힌 clock-out 하나가 이 매장 급여를
        영구히 막고**, 그 매장 row 는 이 콘솔 스코프 밖이라 매니저가 고칠 수도 없다.
        같은 매장의 열린 근무는 게이트 ②(store 스코프, 날짜가 실려 해소 가능)가 잡고,
        다른 매장 것은 그 매장이 닫아 confirm 할 때 같은 게이트가 잡는다.
        """
        store_user_ids = (
            select(Attendance.user_id)
            .where(
                Attendance.store_id.in_(store_ids),
                Attendance.work_date >= period.start_date,
                Attendance.work_date <= period.end_date,
                Attendance.status != "cancelled",
                Attendance.user_id.is_not(None),
                Attendance.clock_in.is_not(None),
            )
            .distinct()
        )
        scan_start = period.start_date - timedelta(days=_OVERLAP_SCAN_MARGIN_DAYS)
        scan_end = period.end_date + timedelta(days=_OVERLAP_SCAN_MARGIN_DAYS)
        rows = (
            await db.execute(
                select(
                    Attendance.id,
                    Attendance.user_id,
                    Attendance.work_date,
                    Attendance.clock_in,
                    Attendance.clock_out,
                ).where(
                    Attendance.organization_id == period.organization_id,
                    Attendance.user_id.in_(store_user_ids),
                    Attendance.work_date >= scan_start,
                    Attendance.work_date <= scan_end,
                    Attendance.status != "cancelled",
                    Attendance.clock_in.is_not(None),
                    Attendance.clock_out.is_not(None),
                )
            )
        ).all()
        if not rows:
            return {}

        from app.utils.attendance_overlap import overlapping_keys_from_rows

        now = datetime.now(timezone.utc)
        by_user: dict[UUID, list[tuple]] = {}
        dates_by_id: dict[UUID, tuple[UUID, date]] = {}
        for att_id, user_id, work_date, clock_in, clock_out in rows:
            by_user.setdefault(user_id, []).append((att_id, clock_in, clock_out))
            dates_by_id[att_id] = (user_id, work_date)

        pairs: list[tuple[UUID, date]] = []
        for user_id, user_rows in by_user.items():
            for att_id in overlapping_keys_from_rows(user_rows, now=now):
                _uid, work_date = dates_by_id[att_id]
                # 창 밖(경계 너머) row 는 짝을 찾기 위해서만 읽었다 — 보고는 안 한다.
                if period.start_date <= work_date <= period.end_date:
                    pairs.append((_uid, work_date))
        # 같은 (user, date) 가 두 row 에서 나오므로 중복 제거
        return self._group_dates(sorted(set(pairs), key=lambda p: (str(p[0]), p[1])))

    async def _open_shift_dates(
        self, db: AsyncSession, period: PayPeriod, store_ids: list[UUID]
    ) -> dict[UUID, list[date]]:
        """게이트 ② — clock_out 없는 열린 근무 (user → 날짜 목록, 정렬)."""
        result = await db.execute(
            select(Attendance.user_id, Attendance.work_date)
            .where(
                Attendance.store_id.in_(store_ids),
                Attendance.work_date >= period.start_date,
                Attendance.work_date <= period.end_date,
                Attendance.status != "cancelled",
                Attendance.user_id.is_not(None),
                Attendance.clock_in.is_not(None),
                Attendance.clock_out.is_(None),
            )
            .distinct()
        )
        return self._group_dates(result.all())

    @staticmethod
    def _group_dates(pairs: list) -> dict[UUID, list[date]]:
        grouped: dict[UUID, list[date]] = {}
        for user_id, work_date in pairs:
            grouped.setdefault(user_id, []).append(work_date)
        for dates in grouped.values():
            dates.sort()
        return grouped

    @staticmethod
    async def _load_names(
        db: AsyncSession, user_ids: set[UUID]
    ) -> dict[UUID, str]:
        """preview 행에 없는 사용자(예: 게이트 전용 검출)의 표시 이름 로드."""
        users = (
            (await db.execute(select(User).where(User.id.in_(user_ids))))
            .scalars()
            .all()
        )
        return {u.id: display_name(u) for u in users}

    async def _multi_store_items(
        self,
        db: AsyncSession,
        period: PayPeriod,
        store_ids: list[UUID],
        rows: list[PayrollPreviewRow],
        names: dict[UUID, str],
    ) -> list[ConfirmGateItem]:
        """게이트 ⑤ — org 합산 주간 정합 체크 (모듈 docstring 의 한계 참조).

        group 스코프 전환 후: 그룹 **안** 겸업은 계산 엔진이 법인 합산으로 직접
        계산하므로 여기서 걸지 않는다 — 그룹 매장들을 하나의 버킷으로 합쳐
        평가해서, **그룹 밖(cross-group=다른 법인)** 매장과의 조합만 남긴다.
        (다른 법인은 별개 고용주라 자동 합산 지급이 오히려 틀리다 — v1 은
        기록 정리/수동 처리 유도라는 원래 의도 그대로.)
        """
        from app.services.attendance_service import (
            attendance_service,
            compute_net_work_minutes,
        )

        user_ids = [r.user_id for r in rows]
        if not user_ids:
            return []
        weeks = workweeks_touching(period.start_date, period.end_date)
        scan_start = weeks[0][0]  # 앞으로 걸친 주 포함 (C4 와 동일 경계)
        scan_end = period.end_date  # 뒤로 걸친 주 초과분은 다음 기간 몫

        attendances = (
            (
                await db.execute(
                    select(Attendance).where(
                        Attendance.organization_id == period.organization_id,
                        Attendance.user_id.in_(user_ids),
                        Attendance.work_date >= scan_start,
                        Attendance.work_date <= scan_end,
                        Attendance.status != "cancelled",
                        Attendance.store_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        # 전부 그룹 안 근무면 합산 체크 자체가 불필요 — 엔진이 직접 계산한다.
        group_store_ids = set(store_ids)
        if all(att.store_id in group_store_ids for att in attendances):
            return []

        breaks_map = await attendance_service._load_breaks_map(
            db, [a.id for a in attendances]
        )
        # 그룹 매장들은 하나의 버킷으로 합친다 — 그룹 안 조합은 위반이 아니다.
        _GROUP_BUCKET = period.store_group_id
        per_user_week: dict[tuple[UUID, date], dict[UUID, dict[date, int]]] = {}
        for att in attendances:
            net = compute_net_work_minutes(att, breaks_map.get(att.id, []))
            if net is None:
                continue  # 열린 근무 — 해당 매장 게이트 ② 몫 (한계 문서화)
            wk = week_start_for(att.work_date)
            bucket = (
                _GROUP_BUCKET if att.store_id in group_store_ids else att.store_id
            )
            day_map = per_user_week.setdefault((att.user_id, wk), {}).setdefault(
                bucket, {}
            )
            day_map[att.work_date] = day_map.get(att.work_date, 0) + net

        items: list[ConfirmGateItem] = []
        for (user_id, wk), by_store in sorted(
            per_user_week.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])
        ):
            for _kind, detail in evaluate_org_week_minutes(by_store):
                items.append(
                    ConfirmGateItem(
                        user_id=user_id,
                        member_name=names.get(user_id),
                        dates=[wk],
                        message=f"Week of {wk}: {detail}",
                    )
                )
        return items

    # ── 동결 ─────────────────────────────────────────────────────

    async def _freeze_entries(
        self,
        db: AsyncSession,
        period: PayPeriod,
        rows: list[PayrollPreviewRow],
    ) -> list[PayrollEntry]:
        """preview 행 → payroll_entries INSERT (revision 0, flush 까지만).

        스냅샷 원천: member_name/empid/crewid 는 preview 가 이미 display_name /
        org_member_stores(이 store)/org_members 에서 조립한 값 그대로.
        breakdown 합계 == 스칼라 검증에 실패하면 500 으로 중단 (아무것도 동결 안 됨).
        """
        member_ids: dict[UUID, UUID] = {}
        if rows:
            result = await db.execute(
                select(OrgMember.user_id, OrgMember.id).where(
                    OrgMember.organization_id == period.organization_id,
                    OrgMember.user_id.in_([r.user_id for r in rows]),
                )
            )
            member_ids = {user_id: member_id for user_id, member_id in result.all()}

        entries: list[PayrollEntry] = []
        for row in rows:
            problems = verify_row_consistency(row)
            if problems:
                await db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=(
                        "Payroll totals failed the internal consistency check "
                        f"for {row.member_name}: {'; '.join(problems)}. Nothing "
                        "was confirmed. Please retry — if this keeps happening, "
                        "report it as a bug."
                    ),
                )
            entry = PayrollEntry(
                pay_period_id=period.id,
                organization_id=period.organization_id,
                # group 스코프 entry 는 매장 귀속이 없다 (breakdown.days 가 원천)
                store_id=period.store_id,
                user_id=row.user_id,
                org_member_id=member_ids.get(row.user_id),
                empid=row.empid,
                crewid=row.crewid,
                member_name=row.member_name,
                revision=0,
                regular_minutes=row.regular_minutes,
                ot_minutes=row.ot_minutes,
                dt_minutes=row.dt_minutes,
                regular_pay=row.regular_pay,
                ot_pay=row.ot_pay,
                dt_pay=row.dt_pay,
                penalty_pay=row.penalty_pay,
                bonus_pay=row.bonus_pay,
                card_tips=row.card_tips,
                gross_pay=row.gross_pay,
                calc_version=row.breakdown.calc_version,
                breakdown=row.breakdown.model_dump(mode="json"),
            )
            db.add(entry)
            entries.append(entry)
        await db.flush()
        return entries

    async def _stamp_events(
        self, db: AsyncSession, period: PayPeriod, store_ids: list[UUID]
    ) -> int:
        """범위 내 유효(non-voided)·미동결 이벤트에 pay_period_id 스탬프.

        voided 이벤트는 스탬프하지 않는다 — 조건 소멸 기록은 open 라이프사이클에
        남고, 이후 재감지 revive 도 frozen 이 아니므로 계속 가능하다.
        """
        result = await db.execute(
            update(PayrollEvent)
            .where(
                PayrollEvent.store_id.in_(store_ids),
                PayrollEvent.work_date >= period.start_date,
                PayrollEvent.work_date <= period.end_date,
                PayrollEvent.voided_at.is_(None),
                PayrollEvent.pay_period_id.is_(None),
            )
            .values(pay_period_id=period.id)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount or 0


# 싱글턴 인스턴스 — Singleton instance
payroll_confirm_service: PayrollConfirmService = PayrollConfirmService()
