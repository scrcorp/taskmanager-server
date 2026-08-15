"""Payroll Calc Service — CA 분류 + 금액 계산 엔진 (Payroll v1 Phase 2).

기간 미리보기(preview_period)의 단일 조립 지점. 원천은 전부 기존 모듈 재사용:
    - 일 net 분: attendance_service.compute_net_work_minutes (C1 단일 공식)
    - 시급: rate_service.rate_at (3단 resolver — 급여는 이것만 읽는다)
    - 반월/주 캘린더: payroll_period_service (period_bounds/workweeks, C3/C4)
    - 이벤트: payroll_event_service (penalty 재감지 + 분류 이벤트 upsert)
    - 팁: payroll_period_service.card_tips_for_period (C6, 분배 공식 fork 금지)

분류 규칙 (C2, CA):
    1) 일별 — 12h 초과 = DT(2x), 8~12h = daily OT(1.5x)
    2) 7일 연속 — Sun–Sat 주 7일 전부 근무 시 7일째는 8h 이내 1.5x, 초과 2x.
       일별 결과와 시간당 높은 분류로 병합 (0~8h: reg→OT, 8h~: OT→DT)
    3) 주간 — 주(Sun–Sat) straight-time(비 OT/DT) 분 누적이 40h 를 넘는 순간부터
       regular → weekly OT 재분류 (이중계상 금지 — OT/DT 로 이미 분류된 분은
       40h 카운트에 넣지 않는다)

경계 걸친 주 (C4 / 계산 규칙 3):
    - 기간 시작에 걸친 주는 전기 일자 데이터를 **분류 판정에만** 합산, 지급은
      현 기간 일자만. 전기가 confirmed 면 frozen payroll_entries.breakdown.days 가
      유일 원천 (live attendance 무시), open/미존재면 live attendance.
    - 기간 끝에 걸친 주는 기간 내 일자만으로 판정해도 결과가 같다 — weekly OT
      초과분은 시간순 누적이라 뒤(다음 기간) 일자에만 떨어지고, 7일째도 다음
      기간 일자다. (다음 기간 계산이 이 기간을 전기로 합산한다.)

rate (계산 규칙 1):
    - 일별 적용 rate = rate_at(member, store, work_date)
    - 한 주에 서로 다른 rate 가 2개 이상이면 OT/DT premium base 는 그 주의
      분-가중평균 regular rate (FLSA 표준). 아니면 그날 rate.
      v1 단순화: 가중평균 = Σ(rate × net분) / Σ(net분), rate 미상(≤0/None) 일 제외.

라운딩 (스펙 §5):
    - 분은 정수 유지 (C7). 금액은 구간(segment)별 계산 후 합산 — 센트 반올림은
      구간 레벨(reg/ot/dt 각각) 1회, totals 는 반올림된 구간 금액의 정확한 합.
      → breakdown 합계 == 스칼라 컬럼 검증(confirm)이 정확히 성립.
    - breakdown.days 의 금액(*_amount)은 **표시용 정보값**이라 하루 단위로
      센트 반올림한다. 같은 공식(day_amounts)을 쓰지만 반올림 시점이 달라
      일별 합이 구간 합과 센트 단위로 어긋날 수 있다 — canonical 은 구간이고
      confirm 검증도 구간 기준이다 (일별 금액은 검증 대상 아님).

트랜잭션 규칙:
    - preview_period 는 commit 하지 않는다 (이벤트 upsert 는 flush 만) —
      호출자(라우터/Phase 3 confirm)의 트랜잭션이 소유. 반복 호출 멱등.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Sequence
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.payroll_rules import (
    CA_DAILY_DT_HOURS,
    CA_DAILY_OT_HOURS,
    DAY_PENALTY_MAX_HOURS,
    MINIMUM_WAGE,
    PENALTY_EVENT_KINDS,
    PENALTY_HOURS,
    WEEKLY_OT_HOURS,
)
from app.models.attendance import Attendance
from app.models.org_member import OrgMember, OrgMemberStore
from app.models.organization import Store
from app.models.payroll import PayPeriod, PayrollEntry
from app.models.tip import TipPeriod
from app.models.user import User
from app.schemas.payroll import (
    CALC_VERSION,
    VALIDATION_BELOW_MINIMUM_WAGE,
    VALIDATION_OPEN_SHIFT,
    VALIDATION_OVERLAPPING_ATTENDANCE,
    VALIDATION_RATE_MISSING,
    VALIDATION_TIP_PERIOD_NOT_CONFIRMED,
    VALIDATION_UNCONFIRMED_AUTO_CLOCKOUT,
    ContextDay,
    DayDetail,
    EntryBreakdown,
    PayrollPreviewRow,
    PenaltyLine,
    PreviewValidation,
    RateSegment,
    WorkedBreak,
    WorkedShift,
)
from app.services.payroll_event_service import ClassifiedDay, payroll_event_service
from app.services.payroll_period_service import (
    payroll_period_service,
    prev_period_bounds,
    week_start_for,
    workweeks_touching,
)
from app.services.rate_service import rate_service
from app.utils.exceptions import BadRequestError
from app.utils.names import display_name

_CENT = Decimal("0.01")

# 분류 임계 (분 단위) — 상수 모듈의 시간 값에서 유도
_DAILY_OT_MIN = CA_DAILY_OT_HOURS * 60  # 480
_DAILY_DT_MIN = CA_DAILY_DT_HOURS * 60  # 720
_WEEKLY_OT_MIN = WEEKLY_OT_HOURS * 60  # 2400

# 자동퇴근 anomaly 코드 (attendance_cron_service 가 기록)
_ANOMALY_AUTO_CLOCKED_OUT = "auto_clocked_out"


def _q(amount: Decimal) -> Decimal:
    """센트 반올림 1회 (ROUND_HALF_UP — rate_service._as_money 관례 일치)."""
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# 순수 분류 엔진 — DB 없음 (unit test 대상)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeekDay:
    """classify_week 입력 — 한 주 안의 근무일 1개.

    live 일: net_minutes = C1 net 합 (split shift 합산), rate = rate_at(그날).
    frozen 일(전기 confirmed, 계산 규칙 3): breakdown.days 에서 온 분류 확정값 —
    재분류하지 않고 그대로 통과시키되, straight-time(frozen_regular)은 주 40h
    누적에, net 은 7일 연속 판정과 가중평균에 반영한다.
    """

    work_date: date
    net_minutes: int
    rate: Decimal | None = None
    frozen: bool = False
    frozen_regular: int = 0
    frozen_ot: int = 0
    frozen_dt: int = 0


@dataclass(frozen=True)
class DayClassification:
    """classify_week 출력 — 일 1개의 병합 최종 분류 + 이벤트용 성분.

    regular/ot/dt 는 지급용 병합 최종값 (ot = daily + weekly + 7일째 ≤8h,
    dt = daily >12h + 7일째 >8h). daily_ot/daily_dt/weekly_ot/seventh_day 는
    payroll_events 표기용 성분 — daily_* 는 순수 일별 규칙 산출값이라
    7일째 승격분을 포함하지 않는다 (reason 문구 정확성).
    """

    work_date: date
    net_minutes: int
    regular_minutes: int
    ot_minutes: int
    dt_minutes: int
    daily_ot_minutes: int = 0
    daily_dt_minutes: int = 0
    weekly_ot_minutes: int = 0
    seventh_day: bool = False
    frozen: bool = False
    rate: Decimal | None = None


def classify_week(days: Sequence[WeekDay]) -> list[DayClassification]:
    """한 Sun–Sat 주의 일별 분류 (C2 병합 규칙의 단일 구현).

    적용 순서: ① 일별(>12h DT, 8~12h OT) → ② 7일 연속(7일째 ≤8h→1.5x, >8h→2x,
    시간당 높은 분류로 병합) → ③ 주간(straight-time 누적 40h 초과분 → weekly OT).
    frozen 일은 재분류 없이 통과 (straight-time 누적/7일 판정에는 참여).

    Args:
        days: 같은 Sun–Sat 주에 속한 근무일 목록 (빈 목록 허용, 순서 무관)

    Returns:
        work_date 오름차순 분류 목록

    Raises:
        ValueError: 서로 다른 주의 날짜가 섞였거나 날짜가 중복될 때
    """
    if not days:
        return []
    ordered = sorted(days, key=lambda d: d.work_date)
    anchor = week_start_for(ordered[0].work_date)
    seen: set[date] = set()
    for d in ordered:
        if week_start_for(d.work_date) != anchor:
            raise ValueError("classify_week requires days from a single Sun-Sat week")
        if d.work_date in seen:
            raise ValueError(f"duplicate work_date in week input: {d.work_date}")
        seen.add(d.work_date)

    # 7일 연속 판정 — 주 7일 전부 근무(net>0)했을 때만, 7일째 = 주의 마지막 날.
    worked_dates = {d.work_date for d in ordered if d.net_minutes > 0}
    seventh_date: date | None = None
    if len(worked_dates) == 7:
        seventh_date = anchor + timedelta(days=6)

    results: list[DayClassification] = []
    straight_cum = 0  # 주간 40h 카운터 — straight-time(비 OT/DT) 분만 누적
    for d in ordered:
        if d.frozen:
            # 계산 규칙 3: 전기 confirmed 일은 frozen 분류가 유일 원천 — 통과만.
            straight_cum += d.frozen_regular
            results.append(
                DayClassification(
                    work_date=d.work_date,
                    net_minutes=d.net_minutes,
                    regular_minutes=d.frozen_regular,
                    ot_minutes=d.frozen_ot,
                    dt_minutes=d.frozen_dt,
                    frozen=True,
                    rate=d.rate,
                )
            )
            continue

        net = d.net_minutes
        # ① 일별 규칙 (이벤트 표기용 성분은 여기 값 그대로)
        daily_dt = max(0, net - _DAILY_DT_MIN)
        daily_ot = max(0, min(net, _DAILY_DT_MIN) - _DAILY_OT_MIN)

        is_seventh = seventh_date is not None and d.work_date == seventh_date
        if is_seventh:
            # ② 7일째 병합 — 0~8h: max(reg, 1.5x)=OT / 8h~: max(1.5x, 2x)=2x
            ot = min(net, _DAILY_OT_MIN)
            dt = max(0, net - _DAILY_OT_MIN)
        else:
            ot = daily_ot
            dt = daily_dt
        regular = net - ot - dt

        # ③ 주간 규칙 — straight-time 누적 40h 초과분만 weekly OT 로 재분류
        straight_cum += regular
        weekly_ot = min(regular, max(0, straight_cum - _WEEKLY_OT_MIN))
        regular -= weekly_ot
        ot += weekly_ot

        results.append(
            DayClassification(
                work_date=d.work_date,
                net_minutes=net,
                regular_minutes=regular,
                ot_minutes=ot,
                dt_minutes=dt,
                daily_ot_minutes=daily_ot,
                daily_dt_minutes=daily_dt,
                weekly_ot_minutes=weekly_ot,
                seventh_day=is_seventh,
                rate=d.rate,
            )
        )
    return results


def ot_base_rate_for_week(days: Sequence[WeekDay]) -> Decimal | None:
    """주의 OT/DT premium base — 멀티 rate 주면 분-가중평균, 아니면 None.

    계산 규칙 1: rate 변경이 낀 주의 premium 은 가중평균 regular rate.
    가중평균 = Σ(rate × net분) / Σ(net분). rate 미상(None/≤0) 또는 net=0 인
    일은 표본에서 제외. 반환 None = "그날 rate 를 그대로 base 로 사용".
    반환값은 비반올림 Decimal — 센트 반올림은 구간 금액에서 1회만.
    """
    rated = [
        (d.rate, d.net_minutes)
        for d in days
        if d.net_minutes > 0 and d.rate is not None and d.rate > 0
    ]
    if len({r for r, _ in rated}) <= 1:
        return None
    total_minutes = sum(m for _, m in rated)
    weighted = sum((r * m for r, m in rated), Decimal("0"))
    return weighted / Decimal(total_minutes)


def allocate_penalty_hours(event_count: int) -> list[int]:
    """일 안의 penalty 이벤트별 부과 시간(h) 배분 — 일 상한 2h 클램프 (C5).

    이벤트당 PENALTY_HOURS(1h)씩, 합계가 DAY_PENALTY_MAX_HOURS(2h)를 넘으면
    이후 이벤트는 0h (라인은 남겨 사유는 표시). v1 은 kind 가 2개뿐이라
    상한이 실제로 깎는 일은 없지만 규칙은 여기 한 곳에 둔다.
    """
    hours: list[int] = []
    used = 0
    for _ in range(event_count):
        grant = min(PENALTY_HOURS, max(0, DAY_PENALTY_MAX_HOURS - used))
        hours.append(grant)
        used += grant
    return hours


def parse_frozen_breakdown(breakdown: dict) -> EntryBreakdown:
    """동결된 entry.breakdown(JSONB dict) → 계약 모델 (calc_version 검증 포함).

    Raises:
        BadRequestError: calc_version 이 현재 엔진과 다르거나 파싱 불가일 때
            (동결 포맷이 바뀌면 reader 를 함께 올려야 한다 — 조용한 오독 방지)
    """
    try:
        parsed = EntryBreakdown.model_validate(breakdown)
    except Exception as exc:  # pydantic ValidationError 포함
        raise BadRequestError(f"Frozen payroll breakdown is unreadable: {exc}") from exc
    if parsed.calc_version != CALC_VERSION:
        raise BadRequestError(
            f"Frozen payroll breakdown calc_version {parsed.calc_version} is not "
            f"supported by this engine (expected {CALC_VERSION})"
        )
    return parsed


def frozen_day_to_week_day(day: DayDetail) -> WeekDay:
    """frozen breakdown 의 일별 상세 → 분류 입력 (재분류 금지 통과 모드)."""
    net = day.regular_minutes + day.ot_minutes + day.dt_minutes
    return WeekDay(
        work_date=day.work_date,
        net_minutes=net,
        rate=day.applied_rate,
        frozen=True,
        frozen_regular=day.regular_minutes,
        frozen_ot=day.ot_minutes,
        frozen_dt=day.dt_minutes,
    )


# ---------------------------------------------------------------------------
# 구간(segment) 누적기 — rate 별 금액 집계 (라운딩 규칙의 구현 지점)
# ---------------------------------------------------------------------------


def day_amounts(
    day: DayClassification, eff_rate: Decimal, ot_base: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    """일 1개의 카테고리별 **비반올림** 금액 (regular, ot, dt).

    구간 누적과 일별 표시 금액이 갈라지지 않도록 공식은 여기 한 곳뿐이다.
    정규는 그날 적용 rate, OT/DT premium 은 그 주의 base (멀티 rate 주면
    분-가중평균, 계산 규칙 1). 반올림 시점은 호출자가 정한다 — 구간은
    누적 후 1회(canonical), 일별 표시는 하루 단위(정보값).
    """
    return (
        eff_rate * day.regular_minutes / Decimal(60),
        ot_base * Decimal("1.5") * day.ot_minutes / Decimal(60),
        ot_base * Decimal(2) * day.dt_minutes / Decimal(60),
    )


class _SegmentAccumulator:
    """같은 적용 rate 일들의 분/금액 누적 → 마감 시 카테고리별 센트 반올림 1회."""

    def __init__(self, rate: Decimal) -> None:
        self.rate = rate
        self.regular_minutes = 0
        self.ot_minutes = 0
        self.dt_minutes = 0
        self._reg_amount = Decimal("0")
        self._ot_amount = Decimal("0")
        self._dt_amount = Decimal("0")

    def add_day(
        self, day: DayClassification, eff_rate: Decimal, ot_base: Decimal
    ) -> None:
        self.regular_minutes += day.regular_minutes
        self.ot_minutes += day.ot_minutes
        self.dt_minutes += day.dt_minutes
        reg, ot, dt = day_amounts(day, eff_rate, ot_base)
        self._reg_amount += reg
        self._ot_amount += ot
        self._dt_amount += dt

    def finalize(self) -> tuple[RateSegment, Decimal, Decimal, Decimal]:
        """(segment, reg_pay, ot_pay, dt_pay) — 각 카테고리 센트 반올림 1회."""
        reg = _q(self._reg_amount)
        ot = _q(self._ot_amount)
        dt = _q(self._dt_amount)
        segment = RateSegment(
            rate=self.rate,
            regular_minutes=self.regular_minutes,
            ot_minutes=self.ot_minutes,
            dt_minutes=self.dt_minutes,
            amount=reg + ot + dt,
        )
        return segment, reg, ot, dt


# ---------------------------------------------------------------------------
# 서비스
# ---------------------------------------------------------------------------


class PayrollCalcService:
    """기간 미리보기 계산 엔진 — attendance → 분류 → 이벤트 → 금액 → 팁 조립."""

    async def preview_period(
        self,
        db: AsyncSession,
        store_id: UUID,
        period: PayPeriod,
        *,
        mutate_events: bool = True,
    ) -> list[PayrollPreviewRow]:
        """store + pay period 의 직원별 미리보기 — 동결 없는 순수 계산 (멱등).

        payroll_events 는 부수효과로 upsert 된다 (open 기간 재계산 라이프사이클,
        flush 만 — commit 은 호출자 소유). entries 저장은 Phase 3 confirm.

        Args:
            mutate_events: False 면 이벤트를 전혀 쓰지 않는 **읽기 전용 계산**
                (upsert/void 없음, 저장된 이벤트를 읽기만) — 확정 기간을
                되짚어보는 백필/감사 용도. 동결된 기간의 이벤트를 건드리면
                안 되므로 그 경로는 반드시 False 로 호출한다.

        Returns:
            member_name → user_id 순 정렬된 행 목록. 행 대상 = 기간 내
            attendance 가 있는 직원 ∪ 카드팁 ≠ 0 직원 ∪ 유효 penalty 이벤트 직원.
        """
        p_start, p_end = period.start_date, period.end_date
        weeks = workweeks_touching(p_start, p_end)
        calc_start = weeks[0][0]  # 첫 주 일요일 (≤ p_start — 경계 걸친 주 포함)

        # ── 전기(prior period) 소스 결정 (계산 규칙 3) ────────────────
        prior_period: Optional[PayPeriod] = None
        prior_frozen = False
        frozen_days_by_user: dict[UUID, dict[date, DayDetail]] = {}
        if calc_start < p_start:
            prev_start, _ = prev_period_bounds(p_start)
            prior_period = await payroll_period_service.get_period(
                db, store_id=store_id, date_in_period=prev_start
            )
            prior_frozen = prior_period is not None and prior_period.status == "confirmed"
            if prior_frozen:
                frozen_days_by_user = await self._load_frozen_days(
                    db, prior_period, from_date=calc_start
                )

        # ── live attendance 로드 ─────────────────────────────────────
        # 전기가 confirmed 면 frozen 이 유일 원천 — live 는 현 기간만 읽는다.
        live_start = p_start if prior_frozen else calc_start
        day_data = await self._load_live_days(db, store_id, live_start, p_end)
        # 검증 플래그는 현 기간 일자만 (경계 걸친 주의 전기 일자는 전기 preview 몫)
        open_shift_dates = self._clip_dates(day_data.open_shift_dates, p_start, p_end)
        overlap_dates_map = self._clip_dates(day_data.overlap_dates, p_start, p_end)
        auto_clockout_dates = self._clip_dates(
            day_data.auto_clockout_dates, p_start, p_end
        )

        # ── 행 대상 사용자 ───────────────────────────────────────────
        tips_map = await payroll_period_service.card_tips_for_period(
            db, store_id=store_id, period=period
        )
        candidate_ids: set[UUID] = set()
        for user_id, days in day_data.nets.items():
            if any(p_start <= d <= p_end for d in days):
                candidate_ids.add(user_id)
        candidate_ids |= {uid for uid, v in tips_map.items() if v != 0}
        candidate_ids |= set(open_shift_dates)
        candidate_ids |= set(auto_clockout_dates)
        candidate_ids |= set(overlap_dates_map)

        # ── 분류 + 금액 (사용자별) ───────────────────────────────────
        org_id = period.organization_id
        members = await self._load_members(db, candidate_ids, org_id, store_id)
        all_classified: list[ClassifiedDay] = []
        computed: dict[UUID, dict] = {}
        for user_id in candidate_ids:
            member = members["by_user"].get(user_id)
            result = await self._compute_user(
                db,
                user_id=user_id,
                member=member,
                store_id=store_id,
                org_id=org_id,
                weeks=weeks,
                p_start=p_start,
                p_end=p_end,
                prior_frozen=prior_frozen,
                frozen_days=frozen_days_by_user.get(user_id, {}),
                day_data=day_data,
            )
            computed[user_id] = result
            all_classified.extend(result["classified_days"])

        # ── 이벤트 반영 (분류 → penalty 재감지 → 유효 이벤트 조회) ───
        # 읽기 전용 모드는 upsert/void 를 건너뛰고 저장된 이벤트만 읽는다.
        if mutate_events:
            await payroll_event_service.upsert_classification_events(
                db, store_id, p_start, p_end, all_classified
            )
            await payroll_event_service.detect_and_upsert_events(
                db, store_id, p_start, p_end
            )
        events = await payroll_event_service.list_events(db, store_id, p_start, p_end)
        penalty_by_user: dict[UUID, list] = {}
        for event in events:
            if event.kind in PENALTY_EVENT_KINDS and event.user_id is not None:
                penalty_by_user.setdefault(event.user_id, []).append(event)
        candidate_ids |= set(penalty_by_user)  # 판정 보류 일의 잔존 penalty 도 지급
        for user_id in candidate_ids - set(computed):
            computed[user_id] = self._empty_user_result()

        # 이벤트만으로 추가된 사용자의 member/이름/empid 로드 보강
        extra_ids = candidate_ids - set(members["users"])
        if extra_ids:
            extra = await self._load_members(db, extra_ids, org_id, store_id)
            members["by_user"].update(extra["by_user"])
            members["users"].update(extra["users"])
            members["empid"].update(extra["empid"])

        # ── 팁 기간 (계산 규칙 4) ────────────────────────────────────
        # tip_period_status_for 와 동일 경계 판정 — id 도 필요해 직접 1회 조회.
        tip_period = await db.scalar(
            select(TipPeriod).where(
                TipPeriod.store_id == store_id,
                TipPeriod.start_date == p_start,
                TipPeriod.end_date == p_end,
            )
        )
        tip_confirmed = tip_period is not None and tip_period.status == "confirmed"
        tip_period_id = str(tip_period.id) if tip_period is not None else None

        sources: dict | None = None
        if calc_start < p_start:
            sources = {
                "prior_period_id": str(prior_period.id) if prior_period else None,
                "prior_period_frozen": prior_frozen,
            }

        # ── 행 조립 ──────────────────────────────────────────────────
        rows: list[PayrollPreviewRow] = []
        for user_id in candidate_ids:
            user = members["users"].get(user_id)
            if user is None:
                continue  # 계정 유실 (SET NULL) — 스냅샷 대상 아님
            member = members["by_user"].get(user_id)
            result = computed[user_id]
            row = await self._assemble_row(
                db,
                user=user,
                member=member,
                store_id=store_id,
                empid=members["empid"].get(user_id),
                result=result,
                penalties=penalty_by_user.get(user_id, []),
                card_tips=_q(tips_map.get(user_id, Decimal("0"))),
                tip_period_id=tip_period_id,
                tip_confirmed=tip_confirmed,
                sources=sources,
                open_dates=open_shift_dates.get(user_id, []),
                auto_dates=auto_clockout_dates.get(user_id, []),
                overlap_dates=overlap_dates_map.get(user_id, []),
            )
            rows.append(row)

        rows.sort(key=lambda r: (r.member_name, str(r.user_id)))
        return rows

    # ── 내부: live attendance → 일 단위 데이터 ───────────────────────

    @staticmethod
    def _clip_dates(
        dates_map: dict[UUID, list[date]], start: date, end: date
    ) -> dict[UUID, list[date]]:
        """사용자별 날짜 목록을 [start, end] 로 잘라내고 빈 항목 제거."""
        clipped = {
            uid: [d for d in dates if start <= d <= end]
            for uid, dates in dates_map.items()
        }
        return {uid: dates for uid, dates in clipped.items() if dates}

    @dataclass
    class _LiveDayData:
        """(user, work_date) 일 단위 집계 — split shift 합산 + 검증 플래그."""

        nets: dict[UUID, dict[date, int]]  # 완결(net 계산 가능) 일의 C1 net 합
        attendance_ids: dict[tuple[UUID, date], UUID | None]  # 1:1 이면 id
        open_shift_dates: dict[UUID, list[date]]  # clock_in 有 + clock_out 無
        auto_clockout_dates: dict[UUID, list[date]]  # anomaly auto_clocked_out
        # 시간이 겹친 근무 (D15) — 확정 게이트 ⑦ 의 사전 경고. 판정 규칙은
        # app/utils/attendance_overlap.py 하나뿐이라 게이트와 갈리지 않는다.
        # 여기서는 이 매장 범위만 본다(preview 는 매장 단위) — 게이트는 org 전역이라
        # 매장 preview 에 안 보이던 겹침이 확정 시점에 새로 걸릴 수 있다.
        overlap_dates: dict[UUID, list[date]]
        # 표시용 벽시계 (store-tz) — 지급 판정에는 쓰지 않는다 (분은 C1 net 이 원천)
        shifts: dict[tuple[UUID, date], list[WorkedShift]]
        breaks: dict[tuple[UUID, date], list[WorkedBreak]]

    async def _load_live_days(
        self,
        db: AsyncSession,
        store_id: UUID,
        start: date,
        end: date,
    ) -> "PayrollCalcService._LiveDayData":
        """non-cancelled attendance 를 일 단위로 합산 (C1 net, split shift 합산).

        net 미계산(미퇴근) attendance 는 합산에서 빠지고 open_shift 로 기록 —
        그날 전부 미퇴근이면 일 자체가 nets 에 없다 (지급 불가, 게이트 ② 대상).
        """
        from app.services.attendance_service import (
            attendance_service,
            compute_net_work_minutes,
        )

        attendances = (
            (
                await db.execute(
                    select(Attendance).where(
                        Attendance.store_id == store_id,
                        Attendance.work_date >= start,
                        Attendance.work_date <= end,
                        Attendance.status != "cancelled",
                        Attendance.user_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        breaks_map = await attendance_service._load_breaks_map(
            db, [a.id for a in attendances]
        )

        by_day: dict[tuple[UUID, date], list[Attendance]] = {}
        open_shift: dict[UUID, list[date]] = {}
        auto_out: dict[UUID, list[date]] = {}
        for att in attendances:
            by_day.setdefault((att.user_id, att.work_date), []).append(att)
            if att.clock_in is not None and att.clock_out is None:
                dates = open_shift.setdefault(att.user_id, [])
                if att.work_date not in dates:
                    dates.append(att.work_date)
            if _ANOMALY_AUTO_CLOCKED_OUT in (att.anomalies or []):
                dates = auto_out.setdefault(att.user_id, [])
                if att.work_date not in dates:
                    dates.append(att.work_date)

        store = await db.get(Store, store_id)
        tz_name = (store.timezone if store else None) or "UTC"

        nets: dict[UUID, dict[date, int]] = {}
        att_ids: dict[tuple[UUID, date], UUID | None] = {}
        shifts: dict[tuple[UUID, date], list[WorkedShift]] = {}
        breaks: dict[tuple[UUID, date], list[WorkedBreak]] = {}
        for (user_id, work_date), day_atts in by_day.items():
            day_nets = [
                net
                for att in day_atts
                if (net := compute_net_work_minutes(att, breaks_map.get(att.id, [])))
                is not None
            ]
            if not day_nets:
                continue  # 전부 미퇴근 — 지급 불가 일 (open_shift 가 잡는다)
            nets.setdefault(user_id, {})[work_date] = sum(day_nets)
            att_ids[(user_id, work_date)] = (
                day_atts[0].id if len(day_atts) == 1 else None
            )
            day_shifts, day_breaks = self._clock_windows(
                day_atts, breaks_map, tz_name
            )
            if day_shifts:
                shifts[(user_id, work_date)] = day_shifts
            if day_breaks:
                breaks[(user_id, work_date)] = day_breaks
        # 겹침 — 사용자별로 구간을 모아 한 번에 판정.
        from app.utils.attendance_overlap import overlapping_keys_from_rows

        now_utc = datetime.now(timezone.utc)
        by_user_rows: dict[UUID, list[tuple]] = {}
        att_meta: dict[UUID, tuple[UUID, date]] = {}
        for att in attendances:
            if att.clock_in is None:
                continue
            by_user_rows.setdefault(att.user_id, []).append(
                (att.id, att.clock_in, att.clock_out)
            )
            att_meta[att.id] = (att.user_id, att.work_date)
        overlap: dict[UUID, list[date]] = {}
        for user_id, user_rows in by_user_rows.items():
            for att_id in overlapping_keys_from_rows(user_rows, now=now_utc):
                _uid, wd = att_meta[att_id]
                dates = overlap.setdefault(_uid, [])
                if wd not in dates:
                    dates.append(wd)

        for dates in open_shift.values():
            dates.sort()
        for dates in auto_out.values():
            dates.sort()
        for dates in overlap.values():
            dates.sort()
        return self._LiveDayData(
            nets=nets,
            attendance_ids=att_ids,
            open_shift_dates=open_shift,
            auto_clockout_dates=auto_out,
            overlap_dates=overlap,
            shifts=shifts,
            breaks=breaks,
        )

    @staticmethod
    def _clock_windows(
        day_atts: list[Attendance],
        breaks_map: dict[UUID, list],
        tz_name: str,
    ) -> tuple[list[WorkedShift], list[WorkedBreak]]:
        """그날 attendance/휴게 → store-tz 벽시계 "HH:MM" 목록 (표시 전용).

        저장은 aware UTC instant 라 매장 타임존으로 되돌려야 사람이 아는 시각이
        된다 (자정 넘김 근무는 종료가 다음날 — 시:분만 담으므로 그대로 읽힌다).
        """
        tz = ZoneInfo(tz_name)

        def hhmm(value) -> str | None:
            return value.astimezone(tz).strftime("%H:%M") if value else None

        shifts: list[WorkedShift] = []
        breaks: list[WorkedBreak] = []
        for att in sorted(
            day_atts, key=lambda a: (a.clock_in is None, a.clock_in or a.work_date)
        ):
            start = hhmm(att.clock_in)
            if start is None:
                continue  # 출근 기록이 없으면 표시할 구간도 없다
            shifts.append(WorkedShift(start=start, end=hhmm(att.clock_out)))
            for brk in breaks_map.get(att.id, []):
                brk_start = hhmm(brk.started_at)
                if brk_start is None:
                    continue
                breaks.append(
                    WorkedBreak(
                        start=brk_start,
                        end=hhmm(brk.ended_at),
                        type=brk.break_type,
                    )
                )
        return shifts, breaks

    # ── 내부: 전기 frozen breakdown 로드 (계산 규칙 3) ───────────────

    async def _load_frozen_days(
        self,
        db: AsyncSession,
        prior_period: PayPeriod,
        *,
        from_date: date,
    ) -> dict[UUID, dict[date, DayDetail]]:
        """confirmed 전기 entries 의 breakdown.days 중 from_date 이후 일만 추출.

        같은 (user) 에 revision 이 여럿이면 최신 revision 이 원천 (v1 은 항상 0).
        """
        entries = (
            (
                await db.execute(
                    select(PayrollEntry)
                    .where(
                        PayrollEntry.pay_period_id == prior_period.id,
                        PayrollEntry.user_id.is_not(None),
                    )
                    .order_by(PayrollEntry.revision.asc())
                )
            )
            .scalars()
            .all()
        )
        result: dict[UUID, dict[date, DayDetail]] = {}
        for entry in entries:  # revision 오름차순 — 뒤(최신)가 덮어씀
            parsed = parse_frozen_breakdown(entry.breakdown)
            result[entry.user_id] = {
                d.work_date: d for d in parsed.days if d.work_date >= from_date
            }
        return result

    # ── 내부: 멤버/이름/empid 로드 ───────────────────────────────────

    async def _load_members(
        self,
        db: AsyncSession,
        user_ids: set[UUID],
        org_id: UUID,
        store_id: UUID,
    ) -> dict:
        """{by_user: OrgMember, users: User, empid: int} 맵 일괄 로드."""
        if not user_ids:
            return {"by_user": {}, "users": {}, "empid": {}}
        members = (
            (
                await db.execute(
                    select(OrgMember).where(
                        OrgMember.user_id.in_(user_ids),
                        OrgMember.organization_id == org_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        by_user = {m.user_id: m for m in members}
        users = (
            (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
        )
        empid_rows = await db.execute(
            select(OrgMember.user_id, OrgMemberStore.empid)
            .join(OrgMemberStore, OrgMemberStore.org_member_id == OrgMember.id)
            .where(
                OrgMember.user_id.in_(user_ids),
                OrgMember.organization_id == org_id,
                OrgMemberStore.store_id == store_id,
            )
        )
        return {
            "by_user": by_user,
            "users": {u.id: u for u in users},
            "empid": {r.user_id: r.empid for r in empid_rows},
        }

    # ── 내부: 사용자 1명 분류 + 금액 ─────────────────────────────────

    @staticmethod
    def _empty_user_result() -> dict:
        return {
            "paid_days": [],
            "context_days": [],
            "day_shifts": {},
            "day_breaks": {},
            "week_bases": {},
            "day_rates": {},
            "classified_days": [],
        }

    async def _compute_user(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        member: OrgMember | None,
        store_id: UUID,
        org_id: UUID,
        weeks: list[tuple[date, date]],
        p_start: date,
        p_end: date,
        prior_frozen: bool,
        frozen_days: dict[date, DayDetail],
        day_data: "PayrollCalcService._LiveDayData",
    ) -> dict:
        """사용자 1명의 주 단위 분류 + 주별 OT base 결정.

        Returns:
            paid_days: 현 기간 내 DayClassification (지급 대상)
            context_days: 주 판정에만 합산된 직전 기간 일자 (지급 아님 — 근거용)
            week_bases: {week_start: Decimal | None} — 가중평균 base (None=그날 rate)
            day_rates: {work_date: Decimal | None} — 그날 적용 rate
            classified_days: 이벤트 upsert 입력 (현 기간 내 live 일만)
        """
        user_nets = day_data.nets.get(user_id, {})
        paid_days: list[DayClassification] = []
        context_days: list[ContextDay] = []
        day_shifts: dict[date, list[WorkedShift]] = {}
        day_breaks: dict[date, list[WorkedBreak]] = {}
        week_bases: dict[date, Decimal | None] = {}
        day_rates: dict[date, Decimal | None] = {}
        classified_days: list[ClassifiedDay] = []

        for week_start, week_end in weeks:
            effective_end = min(week_end, p_end)  # 기간 끝 걸친 주 — 기간 내만
            week_days: list[WeekDay] = []
            cursor = week_start
            while cursor <= effective_end:
                if cursor < p_start and prior_frozen:
                    detail = frozen_days.get(cursor)
                    if detail is not None:
                        week_days.append(frozen_day_to_week_day(detail))
                else:
                    net = user_nets.get(cursor)
                    if net is not None:
                        rate = await rate_service.rate_at(
                            db,
                            member,
                            store_id=store_id,
                            on_date=cursor,
                            user_id=user_id,
                            organization_id=org_id,
                        )
                        day_rates[cursor] = rate
                        week_days.append(
                            WeekDay(work_date=cursor, net_minutes=net, rate=rate)
                        )
                cursor += timedelta(days=1)
            if not week_days:
                continue

            classified = classify_week(week_days)
            week_bases[week_start] = ot_base_rate_for_week(week_days)
            for day in classified:
                if day.frozen or not (p_start <= day.work_date <= p_end):
                    # C4: 지급은 현 기간 근무분만. 다만 직전 기간 일자는 이 주의
                    # 40h/7일 판정에 이미 합산됐으므로 근거로 남긴다 (지급 아님).
                    if day.work_date < p_start and day.net_minutes > 0:
                        context_days.append(
                            ContextDay(
                                work_date=day.work_date,
                                net_minutes=day.net_minutes,
                            )
                        )
                    continue
                paid_days.append(day)
                day_shifts[day.work_date] = day_data.shifts.get(
                    (user_id, day.work_date), []
                )
                day_breaks[day.work_date] = day_data.breaks.get(
                    (user_id, day.work_date), []
                )
                classified_days.append(
                    ClassifiedDay(
                        user_id=user_id,
                        work_date=day.work_date,
                        daily_ot_minutes=day.daily_ot_minutes,
                        daily_dt_minutes=day.daily_dt_minutes,
                        weekly_ot_minutes=day.weekly_ot_minutes,
                        seventh_day=day.seventh_day,
                        attendance_id=day_data.attendance_ids.get(
                            (user_id, day.work_date)
                        ),
                    )
                )
        return {
            "paid_days": paid_days,
            "context_days": sorted(context_days, key=lambda d: d.work_date),
            "day_shifts": day_shifts,
            "day_breaks": day_breaks,
            "week_bases": week_bases,
            "day_rates": day_rates,
            "classified_days": classified_days,
        }

    # ── 내부: 행 조립 (segments/penalties/validations/totals) ────────

    async def _assemble_row(
        self,
        db: AsyncSession,
        *,
        user: User,
        member: OrgMember | None,
        store_id: UUID,
        empid: int | None,
        result: dict,
        penalties: list,
        card_tips: Decimal,
        tip_period_id: str | None,
        tip_confirmed: bool,
        sources: dict | None,
        open_dates: list[date],
        auto_dates: list[date],
        overlap_dates: list[date],
    ) -> PayrollPreviewRow:
        paid_days: list[DayClassification] = sorted(
            result["paid_days"], key=lambda d: d.work_date
        )
        week_bases: dict[date, Decimal | None] = result["week_bases"]
        day_rates: dict[date, Decimal | None] = result["day_rates"]
        day_shifts: dict[date, list[WorkedShift]] = result["day_shifts"]
        day_breaks: dict[date, list[WorkedBreak]] = result["day_breaks"]

        # 검증 수집 — 코드당 1건, 날짜 목록으로 요약
        rate_missing_dates: list[date] = []
        below_min_dates: list[date] = []

        segments: dict[Decimal, _SegmentAccumulator] = {}
        day_details: list[DayDetail] = []
        reg_min = ot_min = dt_min = 0
        for day in paid_days:
            rate = day.rate
            if rate is None or rate <= 0:
                rate_missing_dates.append(day.work_date)
            elif rate < MINIMUM_WAGE:
                below_min_dates.append(day.work_date)

            eff_rate = rate if rate is not None and rate > 0 else Decimal("0")
            base = week_bases.get(week_start_for(day.work_date))
            ot_base = base if base is not None else eff_rate

            # 일별 금액 = 표시용 정보값 — 하루 단위로 센트 반올림한다. 구간은
            # 누적 후 1회 반올림(canonical)이라 일별 합과 센트가 어긋날 수 있다.
            raw_reg, raw_ot, raw_dt = day_amounts(day, eff_rate, ot_base)
            day_reg, day_ot, day_dt = _q(raw_reg), _q(raw_ot), _q(raw_dt)
            day_details.append(
                DayDetail(
                    work_date=day.work_date,
                    regular_minutes=day.regular_minutes,
                    ot_minutes=day.ot_minutes,
                    dt_minutes=day.dt_minutes,
                    applied_rate=rate,
                    regular_amount=day_reg,
                    ot_amount=day_ot,
                    dt_amount=day_dt,
                    total_amount=day_reg + day_ot + day_dt,
                    shifts=day_shifts.get(day.work_date, []),
                    breaks=day_breaks.get(day.work_date, []),
                )
            )
            reg_min += day.regular_minutes
            ot_min += day.ot_minutes
            dt_min += day.dt_minutes
            if day.net_minutes <= 0:
                continue  # 0분 일은 금액 구간에 노이즈만 — days 에는 남긴다
            key = rate if rate is not None else Decimal("0")
            segments.setdefault(key, _SegmentAccumulator(key)).add_day(
                day, eff_rate, ot_base
            )

        segment_models: list[RateSegment] = []
        regular_pay = ot_pay = dt_pay = Decimal("0.00")
        for key in sorted(segments):
            segment, reg, ot, dt = segments[key].finalize()
            segment_models.append(segment)
            regular_pay += reg
            ot_pay += ot
            dt_pay += dt

        # penalty 라인 — 일 상한 2h 클램프, 그날 적용 rate (미상이면 rate_at 재조회)
        penalty_lines: list[PenaltyLine] = []
        penalty_pay = Decimal("0.00")
        by_day: dict[date, list] = {}
        for event in sorted(penalties, key=lambda e: (e.work_date, e.kind)):
            by_day.setdefault(event.work_date, []).append(event)
        for work_date in sorted(by_day):
            day_events = by_day[work_date]
            hours = allocate_penalty_hours(len(day_events))
            rate = day_rates.get(work_date)
            if rate is None:
                # 판정 보류 일(미퇴근 재오픈 등)의 잔존 penalty — rate 재조회
                rate = await rate_service.rate_at(
                    db,
                    member,
                    store_id=store_id,
                    on_date=work_date,
                    user_id=user.id,
                    organization_id=user.organization_id,
                )
            if rate is None or rate <= 0:
                if work_date not in rate_missing_dates:
                    rate_missing_dates.append(work_date)
                rate = Decimal("0")
            for event, grant in zip(day_events, hours):
                amount = _q(rate * grant)
                penalty_lines.append(
                    PenaltyLine(
                        work_date=work_date,
                        kind=event.kind,
                        reason=event.reason,
                        amount=amount,
                    )
                )
                penalty_pay += amount

        gross = regular_pay + ot_pay + dt_pay + penalty_pay + card_tips

        validations: list[PreviewValidation] = []
        if rate_missing_dates:
            validations.append(
                PreviewValidation(
                    code=VALIDATION_RATE_MISSING,
                    message=(
                        "Hourly rate is missing or zero on: "
                        + ", ".join(str(d) for d in sorted(rate_missing_dates))
                    ),
                    user_id=user.id,
                )
            )
        if below_min_dates:
            validations.append(
                PreviewValidation(
                    code=VALIDATION_BELOW_MINIMUM_WAGE,
                    message=(
                        f"Applied hourly rate is below minimum wage "
                        f"(${MINIMUM_WAGE}) on: "
                        + ", ".join(str(d) for d in sorted(below_min_dates))
                    ),
                    user_id=user.id,
                )
            )
        if open_dates:
            validations.append(
                PreviewValidation(
                    code=VALIDATION_OPEN_SHIFT,
                    message=(
                        "Open shift without clock-out on: "
                        + ", ".join(str(d) for d in open_dates)
                    ),
                    user_id=user.id,
                )
            )
        if auto_dates:
            validations.append(
                PreviewValidation(
                    code=VALIDATION_UNCONFIRMED_AUTO_CLOCKOUT,
                    message=(
                        "Auto clock-out has not been confirmed on: "
                        + ", ".join(str(d) for d in auto_dates)
                    ),
                    user_id=user.id,
                )
            )
        if overlap_dates:
            validations.append(
                PreviewValidation(
                    code=VALIDATION_OVERLAPPING_ATTENDANCE,
                    message=(
                        "Two shifts overlap in time (the same hours would be paid "
                        "twice) on: " + ", ".join(str(d) for d in overlap_dates)
                    ),
                    user_id=user.id,
                )
            )
        if card_tips != 0 and not tip_confirmed:
            validations.append(
                PreviewValidation(
                    code=VALIDATION_TIP_PERIOD_NOT_CONFIRMED,
                    message=(
                        "Card tips are provisional — the matching tip period "
                        "is not confirmed yet"
                    ),
                    user_id=user.id,
                )
            )

        breakdown = EntryBreakdown(
            calc_version=CALC_VERSION,
            segments=segment_models,
            days=day_details,
            penalties=penalty_lines,
            context_days=result["context_days"],
            tip_period_id=tip_period_id,
            sources=sources,
        )
        return PayrollPreviewRow(
            user_id=user.id,
            member_name=display_name(user),
            empid=empid,
            crewid=member.crewid if member is not None else None,
            regular_minutes=reg_min,
            ot_minutes=ot_min,
            dt_minutes=dt_min,
            regular_pay=regular_pay,
            ot_pay=ot_pay,
            dt_pay=dt_pay,
            penalty_pay=penalty_pay,
            card_tips=card_tips,
            gross_pay=gross,
            breakdown=breakdown,
            validations=validations,
        )


# 싱글턴 인스턴스 — Singleton instance
payroll_calc_service: PayrollCalcService = PayrollCalcService()
