"""Payroll Period Service — 반월 정산 기간 + 팁 집계 (Payroll v1 Phase 2).

pay_periods 라이프사이클의 진입점 (스펙: docs/99_inbox/2026-08-03
payroll-v1-스키마-스펙.md §4, 설계방향 C3/C4/C6 + 2026-08-19 group 스코프 전환 D1~D5):

    - 반월 캘린더 헬퍼: period_bounds_for / prev_period_bounds / workweeks_touching
      pay period 는 팁 사이클과 동일한 반월(1~15 / 16~말일) — cycle_for_date 재사용
      (C6: 카드팁은 pay period 에 맞춰 paycheck 포함, 사이클 경계 일치가 전제)
    - ensure_period: get-or-create. **시스템 생성 전용** — 수동 생성 API 없음
      (스펙 §4: 겹침 원천 차단. 겹침 CHECK 없이 uq(group, start)만으로 충분한 이유)
    - is_date_locked: work_date 가 confirmed 기간 안이면 True — 이후 attendance/
      rate 수정 잠금 enforcement 가 이 함수를 단일 판정으로 사용.
      **호출부는 여전히 store_id 를 넘긴다** — store→group 해석은 여기서 한다
      (락 호출처 5개 파일을 스코프 전환이 건드리지 않게 하는 경계).
    - card_tips_for_period / tip_period_status_for: 팁 도메인 연동.
      팁은 매장 운영 단위(D4)라 store 스코프 그대로 두고, group 기간 계산이
      그룹 내 매장들을 합산한다. 분배 공식은 tip_service.summarize_employee_tips
      가 유일 원천 (fork 금지) — 계산 규칙 4: payroll confirm 은 그룹 내 전 매장의
      대응 tip_period 가 confirmed 여야 한다.

group 스코프 전환 (2026-08-19, D1):
    급여의 법적 주체는 Group(법인). 신규 기간은 (store_group_id, start_date) 로
    생성·조회한다. 전환 전에 확정된 store 스코프 기간은 동결 원장으로만 남고
    (store_id 有 / store_group_id NULL), is_date_locked 가 레거시 행도 함께 본다.

타임존 (D5):
    영업일 라벨은 store tz 기준인데 기간은 group 단위다. 그룹 내 매장 tz 가
    갈리면 주 경계 자체가 모호해지므로 **불일치 시 계산을 거부**한다
    (group_timezone). 대표 tz 필드는 만들지 않는다.

트랜잭션 규칙:
    - 이 모듈의 함수는 commit 하지 않는다 — 호출자(서비스/라우터)의 트랜잭션이 소유.
      ensure_period 는 flush 까지만 (TipService.get_or_create_period 관례 일치).
"""

from __future__ import annotations

from datetime import date as DateType, timedelta
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes.common import GROUP_NOT_FOUND
from app.core.error_codes.payroll import (
    PAYROLL_GROUP_HAS_NO_STORES,
    PAYROLL_GROUP_TIMEZONE_MISMATCH,
)
from app.models.organization import Store, StoreGroup
from app.models.payroll import PayPeriod
from app.models.tip import TipDistribution, TipEntry, TipPeriod
from app.seeds.settings_seed import TIP_MODE_HOURS_PRORATED
from app.services.tip_prorate_service import tip_prorate_service
from app.services.tip_service import cycle_for_date, summarize_employee_tips


# ── 반월 캘린더 헬퍼 (순수 함수) ──────────────────────────────


def period_bounds_for(d: DateType) -> tuple[DateType, DateType]:
    """d 가 속한 pay period 의 (start, end) — 1~15 / 16~말일.

    팁 사이클과 동일 경계 (C6). 공식은 tip_service.cycle_for_date 재사용 — fork 금지.
    """
    return cycle_for_date(d)


def prev_period_bounds(d: DateType) -> tuple[DateType, DateType]:
    """d 가 속한 기간의 직전 기간 (start, end).

    전반(1~15) → 전월 후반(16~말일), 후반 → 같은 달 전반. 월/연 경계 자동 처리.
    """
    start, _ = period_bounds_for(d)
    return period_bounds_for(start - timedelta(days=1))


def week_start_for(d: DateType) -> DateType:
    """d 가 속한 workweek 의 시작 일요일 (C3: Sun–Sat 고정)."""
    # Python weekday(): Mon=0 … Sun=6 → 일요일 시작 offset = (weekday+1) % 7
    return d - timedelta(days=(d.weekday() + 1) % 7)


def workweeks_touching(
    period_start: DateType, period_end: DateType,
) -> list[tuple[DateType, DateType]]:
    """[period_start, period_end] 와 겹치는 Sun–Sat 주 목록 (경계에 걸친 주 포함).

    C4: 주 40h 판정은 기간 경계에 걸친 주 전체 시간을 봐야 하므로,
    기간 밖으로 삐져나가는 첫/마지막 주도 온전한 (일~토) 범위로 돌려준다.
    """
    if period_start > period_end:
        raise ValueError("period_start must be <= period_end")
    weeks: list[tuple[DateType, DateType]] = []
    cursor = week_start_for(period_start)
    while cursor <= period_end:
        weeks.append((cursor, cursor + timedelta(days=6)))
        cursor += timedelta(days=7)
    return weeks


# ── 서비스 ────────────────────────────────────────────────────


class PayrollPeriodService:

    # ── 그룹/매장 해석 ────────────────────────────────────────

    async def group_stores(
        self, db: AsyncSession, store_group_id: UUID
    ) -> list[Store]:
        """그룹 소속 전체 매장 — 폐점 포함 (과거 근태가 급여에 남는다).

        Raises:
            NotFoundError: 그룹이 없을 때
            BadRequestError: 그룹에 매장이 하나도 없을 때 (급여 계산 불가)
        """
        group = await db.get(StoreGroup, store_group_id)
        if group is None:
            raise GROUP_NOT_FOUND()
        stores = list(
            (
                await db.scalars(
                    select(Store).where(Store.group_id == store_group_id)
                )
            ).all()
        )
        if not stores:
            raise PAYROLL_GROUP_HAS_NO_STORES()
        return stores

    @staticmethod
    def group_timezone(stores: list[Store]) -> str:
        """그룹 매장들의 공통 타임존 (D5) — 불일치면 계산 거부.

        영업일 라벨과 주(Sun–Sat) 경계가 tz 로 정해지므로, 한 법인 안에서 tz 가
        갈리면 '같은 주' 의 정의가 매장마다 달라진다. 억지로 대표값을 고르는 대신
        데이터를 고치게 한다 (실데이터는 전부 동일 tz — CA 매장들).
        """
        zones = {(s.timezone or "UTC") for s in stores}
        if len(zones) > 1:
            raise PAYROLL_GROUP_TIMEZONE_MISMATCH(zones=", ".join(sorted(zones)))
        return next(iter(zones))

    async def group_for_store(
        self, db: AsyncSession, store_id: UUID
    ) -> Optional[UUID]:
        """매장 → 소속 그룹 id (없으면 None)."""
        return await db.scalar(select(Store.group_id).where(Store.id == store_id))

    # ── pay_periods 라이프사이클 ──────────────────────────────

    async def ensure_period(
        self,
        db: AsyncSession,
        *,
        store_group_id: UUID,
        date_in_period: DateType,
    ) -> PayPeriod:
        """date 가 속한 pay period 를 가져오거나 새로 만든다 (시스템 생성 전용).

        수동 생성 API 를 두지 않는 것이 겹침 원천 차단 장치 — 새 기간이 필요한
        모든 경로는 반드시 여기를 지난다. 경계는 period_bounds_for 가 유일하게
        결정하므로 uq(store_group_id, start_date) 만으로 겹침이 불가능하다.
        """
        start, end = period_bounds_for(date_in_period)
        existing = await db.scalar(
            select(PayPeriod).where(
                PayPeriod.store_group_id == store_group_id,
                PayPeriod.start_date == start,
            )
        )
        if existing is not None:
            return existing

        group = await db.get(StoreGroup, store_group_id)
        if group is None:
            raise GROUP_NOT_FOUND()

        period = PayPeriod(
            organization_id=group.organization_id,
            store_group_id=store_group_id,
            start_date=start,
            end_date=end,
            status="open",
        )
        db.add(period)
        await db.flush()
        return period

    async def get_period(
        self,
        db: AsyncSession,
        *,
        store_group_id: UUID,
        date_in_period: DateType,
    ) -> Optional[PayPeriod]:
        """date 가 속한 기간 조회 — 없으면 None (자동 생성 안 함)."""
        start, _ = period_bounds_for(date_in_period)
        return await db.scalar(
            select(PayPeriod).where(
                PayPeriod.store_group_id == store_group_id,
                PayPeriod.start_date == start,
            )
        )

    async def get_legacy_store_period(
        self,
        db: AsyncSession,
        *,
        store_id: UUID,
        date_in_period: DateType,
    ) -> Optional[PayPeriod]:
        """전환 전 store 스코프 기간 조회 — 확정 동결분(계산 규칙 3 전기 소스)용."""
        start, _ = period_bounds_for(date_in_period)
        return await db.scalar(
            select(PayPeriod).where(
                PayPeriod.store_id == store_id,
                PayPeriod.start_date == start,
            )
        )

    async def list_periods(
        self,
        db: AsyncSession,
        *,
        store_group_id: UUID,
        range_start: DateType,
        range_end: DateType,
    ) -> list[PayPeriod]:
        """[range_start, range_end] 와 겹치는 그룹 기간 목록 — start_date 오름차순.

        레거시 store 스코프 기간(그룹 소속 매장의 전환 전 확정분)도 함께 돌려준다
        — 과거 확정 원장이 목록에서 사라지면 안 된다.
        """
        store_ids = select(Store.id).where(Store.group_id == store_group_id)
        rows = await db.scalars(
            select(PayPeriod)
            .where(
                (PayPeriod.store_group_id == store_group_id)
                | (PayPeriod.store_id.in_(store_ids)),
                PayPeriod.start_date <= range_end,
                PayPeriod.end_date >= range_start,
            )
            .order_by(PayPeriod.start_date)
        )
        return list(rows.all())

    async def is_date_locked(
        self,
        db: AsyncSession,
        *,
        store_id: UUID,
        work_date: DateType,
    ) -> bool:
        """work_date 가 해당 매장이 속한 법인의 confirmed 기간 안이면 True.

        확정 이후 attendance/rate 수정 잠금 enforcement 의 단일 판정 지점.
        호출부 인터페이스는 store_id 그대로 — 여기서 group 으로 해석한다.
        그룹 기간 확정 = **그룹 내 전 매장** 잠금 (법인 원장 동결, D3 의도된 확대).
        레거시(전환 전 확정) store 스코프 기간도 함께 본다.
        경계 판정은 저장된 기간의 [start, end] 포함 범위 그대로 — 캘린더 재계산에
        의존하지 않아 과거 규칙으로 만든 기간에도 안전하다.
        """
        group_id = await self.group_for_store(db, store_id)
        conditions = [PayPeriod.store_id == store_id]  # 레거시 행
        if group_id is not None:
            conditions.append(PayPeriod.store_group_id == group_id)
        period = await db.scalar(
            select(PayPeriod).where(
                or_(*conditions),
                PayPeriod.start_date <= work_date,
                PayPeriod.end_date >= work_date,
                PayPeriod.status == "confirmed",
            )
        )
        return period is not None

    # ── 팁 도메인 연동 (C6 / 계산 규칙 4) ─────────────────────

    async def card_tips_for_period(
        self,
        db: AsyncSession,
        *,
        store_ids: list[UUID],
        period: PayPeriod,
    ) -> dict[UUID, Decimal]:
        """직원별 paycheck 포함 팁 — 그룹 내 매장들을 합산한다 (D4).

        팁 원장은 매장 단위 그대로다. 매장 설정(payroll.tip_distribution_mode)에
        따라 매장별 원천이 갈리고, 사람 기준으로 합산한다:

        - manual (1안): own card − 나간 분배 전액 + 받은 분배(수락분).
          분배 공식은 tip_service.summarize_employee_tips (4070 과 동일 원천).
          cash tips 는 본인 소지 — paycheck 미포함 (C6).
        - hours_prorated (2안): tip_allocations 의 카드 몫.
          현금 몫은 여기 포함하지 않는다 — C6 과 같은 이유(본인이 그날 가져감)다.
          급여 파일의 earnedtip 은 카드+현금 합계라 export 계층에서 따로 더한다.

        map 에 없는 직원 = 0.
        """
        combined: dict[UUID, Decimal] = {}

        def _add(user_id: UUID, amount: Decimal) -> None:
            combined[user_id] = combined.get(user_id, Decimal("0")) + amount

        for store_id in store_ids:
            if (
                await tip_prorate_service.store_mode(db, store_id)
                == TIP_MODE_HOURS_PRORATED
            ):
                totals = await tip_prorate_service.totals_for_period(
                    db, store_id=store_id,
                    start=period.start_date, end=period.end_date,
                )
                for user_id, t in totals.items():
                    _add(user_id, t["card"])
                continue

            entries = (await db.scalars(
                select(TipEntry).where(
                    TipEntry.store_id == store_id,
                    TipEntry.date >= period.start_date,
                    TipEntry.date <= period.end_date,
                )
            )).all()
            if not entries:
                continue
            dists = (await db.scalars(
                select(TipDistribution).where(
                    TipDistribution.entry_id.in_([e.id for e in entries])
                )
            )).all()

            summary = summarize_employee_tips(entries, dists)
            for emp_id, s in summary.items():
                _add(emp_id, s["own_card"] - s["paid_out"] + s["received_card"])
        return combined

    async def tip_period_status_for(
        self,
        db: AsyncSession,
        *,
        store_ids: list[UUID],
        period: PayPeriod,
    ) -> dict[UUID, Optional[str]]:
        """그룹 내 매장별, pay period 와 같은 날짜범위의 tip_period status.

        {store_id: status | None(미생성)}. 계산 규칙 4: payroll confirm 은 그룹 내
        **전 매장** 이 'confirmed' 여야 한다 (미확정 팁이 급여 스냅샷에 박제되는 것
        방지 — store 스코프 시절에도 매장마다 요구되던 조건과 같은 부담이다).
        enforcement 는 confirm 쪽.
        """
        if not store_ids:
            return {}
        rows = (
            await db.execute(
                select(TipPeriod.store_id, TipPeriod.status).where(
                    TipPeriod.store_id.in_(store_ids),
                    TipPeriod.start_date == period.start_date,
                    TipPeriod.end_date == period.end_date,
                )
            )
        ).all()
        statuses: dict[UUID, Optional[str]] = {sid: None for sid in store_ids}
        for store_id, tp_status in rows:
            statuses[store_id] = tp_status
        return statuses

    @staticmethod
    def aggregate_tip_status(statuses: dict[UUID, Optional[str]]) -> Optional[str]:
        """매장별 tip status → 기간 응답용 요약 하나.

        전부 confirmed → 'confirmed'. 하나라도 미생성(None) → None.
        그 외(open 섞임) → 'open'. 콘솔 사전 표시용 — 게이트 판정은 매장별 원본.
        """
        if not statuses:
            return None
        values = list(statuses.values())
        if all(v == "confirmed" for v in values):
            return "confirmed"
        if any(v is None for v in values):
            return None
        return "open"


payroll_period_service = PayrollPeriodService()
