"""Payroll Period Service — 반월 정산 기간 + 팁 집계 (Payroll v1 Phase 2).

pay_periods 라이프사이클의 진입점 (스펙: docs/99_inbox/2026-08-03
payroll-v1-스키마-스펙.md §4, 설계방향 C3/C4/C6):

    - 반월 캘린더 헬퍼: period_bounds_for / prev_period_bounds / workweeks_touching
      pay period 는 팁 사이클과 동일한 반월(1~15 / 16~말일) — cycle_for_date 재사용
      (C6: 카드팁은 pay period 에 맞춰 paycheck 포함, 사이클 경계 일치가 전제)
    - ensure_period: get-or-create. **시스템 생성 전용** — 수동 생성 API 없음
      (스펙 §4: 겹침 원천 차단. 겹침 CHECK 없이 uq(store, start)만으로 충분한 이유)
    - is_date_locked: work_date 가 confirmed 기간 안이면 True — 이후 attendance/
      rate 수정 잠금 enforcement 가 이 함수를 단일 판정으로 사용
    - card_tips_for_period / tip_period_status_for: 팁 도메인 연동.
      분배 공식은 tip_service.summarize_employee_tips 가 유일 원천 (fork 금지)
      — 계산 규칙 4: payroll confirm 은 대응 tip_period 가 confirmed 여야 한다

트랜잭션 규칙:
    - 이 모듈의 함수는 commit 하지 않는다 — 호출자(서비스/라우터)의 트랜잭션이 소유.
      ensure_period 는 flush 까지만 (TipService.get_or_create_period 관례 일치).
"""

from __future__ import annotations

from datetime import date as DateType, timedelta
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Store
from app.models.payroll import PayPeriod
from app.models.tip import TipDistribution, TipEntry, TipPeriod
from app.services.tip_service import cycle_for_date, summarize_employee_tips
from app.utils.exceptions import NotFoundError


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

    # ── pay_periods 라이프사이클 ──────────────────────────────

    async def ensure_period(
        self,
        db: AsyncSession,
        *,
        store_id: UUID,
        date_in_period: DateType,
    ) -> PayPeriod:
        """date 가 속한 pay period 를 가져오거나 새로 만든다 (시스템 생성 전용).

        수동 생성 API 를 두지 않는 것이 겹침 원천 차단 장치 — 새 기간이 필요한
        모든 경로는 반드시 여기를 지난다. 경계는 period_bounds_for 가 유일하게
        결정하므로 uq(store_id, start_date) 만으로 겹침이 불가능하다.
        """
        start, end = period_bounds_for(date_in_period)
        existing = await db.scalar(
            select(PayPeriod).where(
                PayPeriod.store_id == store_id,
                PayPeriod.start_date == start,
            )
        )
        if existing is not None:
            return existing

        store = await db.scalar(select(Store).where(Store.id == store_id))
        if store is None:
            raise NotFoundError("Store not found")

        period = PayPeriod(
            organization_id=store.organization_id,
            store_id=store_id,
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
        store_id: UUID,
        date_in_period: DateType,
    ) -> Optional[PayPeriod]:
        """date 가 속한 기간 조회 — 없으면 None (자동 생성 안 함)."""
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
        store_id: UUID,
        range_start: DateType,
        range_end: DateType,
    ) -> list[PayPeriod]:
        """[range_start, range_end] 와 겹치는 기간 목록 — start_date 오름차순."""
        rows = await db.scalars(
            select(PayPeriod)
            .where(
                PayPeriod.store_id == store_id,
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
        """work_date 가 해당 매장의 confirmed 기간 안이면 True.

        확정 이후 attendance/rate 수정 잠금 enforcement 의 단일 판정 지점.
        경계 판정은 저장된 기간의 [start, end] 포함 범위 그대로 — 캘린더 재계산에
        의존하지 않아 과거 규칙으로 만든 기간에도 안전하다.
        """
        period = await db.scalar(
            select(PayPeriod).where(
                PayPeriod.store_id == store_id,
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
        store_id: UUID,
        period: PayPeriod,
    ) -> dict[UUID, Decimal]:
        """직원별 paycheck 포함 카드팁 = own card − 나간 분배 전액 + 받은 분배(수락분).

        분배 공식은 tip_service.summarize_employee_tips (4070 과 동일 원천).
        map 에 없는 직원 = 0 (기간 내 entry 도, 수락된 수취도 없음).
        cash tips 는 본인 소지 — paycheck 미포함 (C6).
        """
        entries = (await db.scalars(
            select(TipEntry).where(
                TipEntry.store_id == store_id,
                TipEntry.date >= period.start_date,
                TipEntry.date <= period.end_date,
            )
        )).all()
        if not entries:
            return {}
        dists = (await db.scalars(
            select(TipDistribution).where(
                TipDistribution.entry_id.in_([e.id for e in entries])
            )
        )).all()

        summary = summarize_employee_tips(entries, dists)
        return {
            emp_id: s["own_card"] - s["paid_out"] + s["received_card"]
            for emp_id, s in summary.items()
        }

    async def tip_period_status_for(
        self,
        db: AsyncSession,
        *,
        store_id: UUID,
        period: PayPeriod,
    ) -> Optional[str]:
        """pay period 와 같은 (store, 날짜범위) 의 tip_period status — 없으면 None.

        계산 규칙 4: payroll confirm 은 이 값이 'confirmed' 여야 한다
        (미확정 팁이 급여 스냅샷에 박제되는 것 방지). enforcement 는 confirm 쪽.
        """
        tip_period = await db.scalar(
            select(TipPeriod).where(
                TipPeriod.store_id == store_id,
                TipPeriod.start_date == period.start_date,
                TipPeriod.end_date == period.end_date,
            )
        )
        return tip_period.status if tip_period is not None else None


payroll_period_service = PayrollPeriodService()
