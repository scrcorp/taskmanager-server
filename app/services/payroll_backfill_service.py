"""동결 entry 의 선택 필드(일별 금액 + 경계 주 근거) 백필 — 일회성 보정 유틸.

breakdown 에 additive 로 추가된 필드들이 생기기 전(calc_version=1 유지) 확정된
기간은 그 값이 비어 있다. 근태·rate 원본이 그대로 남아 있으므로 다시 계산해
채워 넣는다:
    - days[].regular/ot/dt/total_amount (일별 금액)
    - context_days (경계 걸친 주의 직전 기간 일자 — 주 40h 판정 근거)

안전 규칙 (이 모듈의 존재 이유):
    1. confirmed 기간만 대상. open 기간은 preview 가 원천이라 백필할 게 없다.
    2. 계산은 **읽기 전용**(preview_period(mutate_events=False)) — 동결된
       payroll_events 를 절대 건드리지 않는다.
    3. 재계산 결과가 동결 스냅샷과 **완전히 일치**할 때만 patch 한다:
       일별 (분류 분, 적용 rate) + rate 구간 (rate/분/금액) + 스칼라 급여.
       하나라도 어긋나면 = 확정 후 원본이 바뀐 것이므로 건드리지 않고 skip
       (사유 기록). 동결본을 조용히 고쳐 쓰지 않는다.
    4. patch 는 breakdown["days"][i] 의 금액 4키 + 최상위 context_days 만 새로
       쓴다 — 나머지 JSON(구간/penalty/tip_period_id/sources/키 순서)은 그대로.
    5. 채울 것이 없는 entry 는 no-op (needs_backfill 참조).

실행: python -m app.scripts.backfill_day_amounts (API 엔드포인트 없음 — 운영
노출 불필요한 일회성 작업).
"""

from __future__ import annotations

import copy
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payroll import PayPeriod, PayrollEntry
from app.schemas.payroll import DayDetail, EntryBreakdown, PayrollPreviewRow
from app.services.payroll_calc_service import (
    parse_frozen_breakdown,
    payroll_calc_service,
)
from app.utils.exceptions import BadRequestError, NotFoundError

# patch 대상 — DayDetail 의 금액 필드 (이 키들만 새로 쓴다)
AMOUNT_KEYS: tuple[str, ...] = (
    "regular_amount",
    "ot_amount",
    "dt_amount",
    "total_amount",
)

# patch 대상 — breakdown 최상위의 경계 주 근거 목록 (통째로 새로 쓴다)
CONTEXT_KEY = "context_days"

# patch 대상 — 일별 근무/휴게 벽시계 (통째로 새로 쓴다)
DAY_WINDOW_KEYS: tuple[str, ...] = ("shifts", "breaks")


def has_day_amounts(breakdown: EntryBreakdown) -> bool:
    """일별 금액이 이미 채워져 있는지 — 하나라도 None 이면 백필 대상."""
    if not breakdown.days:
        return True  # 채울 일자가 없다 = 할 일 없음 (no-op)
    return all(day.total_amount is not None for day in breakdown.days)


def needs_backfill(raw: dict, breakdown: EntryBreakdown) -> bool:
    """이 entry 를 다시 계산해 채울 것이 있는지 (금액 / 경계 주 근거 / 근무 시각).

    목록형 선택 필드(context_days·shifts·breaks)는 **키 유무**로 판단한다 —
    값이 빈 목록인 건 "경계 걸친 주가 없었다", "그날 기록이 없다" 같은 정상
    결과라 값만 봐서는 옛 포맷과 구분할 수 없다. 새 엔진으로 동결하면 빈
    목록이라도 키는 항상 쓰이므로, 키가 없으면 옛 동결본이다.
    """
    if not has_day_amounts(breakdown):
        return True
    if CONTEXT_KEY not in raw:
        return True
    return any(
        key not in day for day in raw.get("days", []) for key in DAY_WINDOW_KEYS
    )


def _day_key(day: DayDetail) -> tuple:
    """일별 동일성 판정 키 — 분류 분 + 적용 rate (금액은 비교 대상 아님)."""
    return (
        day.work_date,
        day.regular_minutes,
        day.ot_minutes,
        day.dt_minutes,
        day.applied_rate,
    )


def _segment_key(breakdown: EntryBreakdown) -> list[tuple]:
    """구간 동일성 판정 키 — rate/분/금액 (동결 시 rate 오름차순)."""
    return [
        (s.rate, s.regular_minutes, s.ot_minutes, s.dt_minutes, s.amount)
        for s in breakdown.segments
    ]


def mismatch_reason(
    entry: PayrollEntry, frozen: EntryBreakdown, row: PayrollPreviewRow
) -> Optional[str]:
    """재계산 행이 동결본과 다르면 사람이 읽는 사유, 같으면 None (순수 함수).

    확정 후 근태/시급이 손대진 기간을 조용히 덮어쓰지 않기 위한 게이트다 —
    불일치는 "고칠 것"이 아니라 "사람이 봐야 할 것"이므로 skip 사유로 남긴다.
    """
    frozen_days = sorted(_day_key(d) for d in frozen.days)
    live_days = sorted(_day_key(d) for d in row.breakdown.days)
    if frozen_days != live_days:
        frozen_dates = {d.work_date for d in frozen.days}
        live_dates = {d.work_date for d in row.breakdown.days}
        if frozen_dates != live_dates:
            missing = sorted(frozen_dates - live_dates)
            added = sorted(live_dates - frozen_dates)
            return (
                "recomputed work dates differ from the frozen snapshot "
                f"(missing: {[str(d) for d in missing]}, "
                f"new: {[str(d) for d in added]}) — attendance changed after "
                "this period was confirmed"
            )
        return (
            "recomputed daily hours or rates differ from the frozen snapshot — "
            "attendance or hourly rate changed after this period was confirmed"
        )

    if _segment_key(frozen) != _segment_key(row.breakdown):
        return (
            "recomputed rate segments differ from the frozen snapshot — "
            "hourly rate history changed after this period was confirmed"
        )

    frozen_pay = (entry.regular_pay, entry.ot_pay, entry.dt_pay)
    live_pay = (row.regular_pay, row.ot_pay, row.dt_pay)
    if frozen_pay != live_pay:
        return (
            f"recomputed pay totals {[str(p) for p in live_pay]} differ from the "
            f"frozen totals {[str(p) for p in frozen_pay]} — leave this entry "
            "to a manual amendment"
        )
    return None


def patched_breakdown(raw: dict, row: PayrollPreviewRow) -> dict:
    """동결 breakdown(JSONB dict) + 재계산 행 → 선택 필드만 채운 새 dict (순수).

    days 는 work_date 로 매칭해 금액 4키 + 근무/휴게 시각을, 최상위에는
    context_days 를 새로 쓴다. 직렬화는 모델의 json dump 를 그대로 써서 새로
    confirm 한 기간과 표기가 어긋나지 않게 한다. 그 밖의 키(구간/penalty/
    tip_period_id/sources)와 분류 값은 불변.
    """
    amounts_by_date = {
        day.work_date.isoformat(): day.model_dump(mode="json")
        for day in row.breakdown.days
    }
    patched = copy.deepcopy(raw)
    for day_dict in patched.get("days", []):
        source = amounts_by_date.get(str(day_dict.get("work_date")))
        if source is None:  # mismatch_reason 통과분이면 도달 불가 (방어)
            continue
        for key in AMOUNT_KEYS + DAY_WINDOW_KEYS:
            day_dict[key] = source[key]
    patched[CONTEXT_KEY] = [
        context.model_dump(mode="json") for context in row.breakdown.context_days
    ]
    return patched


class PayrollBackfillService:
    """확정 기간의 일별 금액 백필 — CLI 에서 호출하는 파사드."""

    async def backfill_frozen_day_amounts(
        self, db: AsyncSession, period_id: UUID
    ) -> dict:
        """기간 1개의 동결 entries 에 일별 금액을 채운다 (commit 은 호출자 소유).

        Returns:
            {"period_id", "start_date", "end_date", "updated", "unchanged",
             "skipped": [{"user_id", "member_name", "reason"}]}

        Raises:
            NotFoundError: 없는 기간
            BadRequestError: confirmed 아닌 기간 (동결본이 없다)
        """
        period = await db.get(PayPeriod, period_id)
        if period is None:
            raise NotFoundError("Pay period not found")
        if period.status != "confirmed":
            raise BadRequestError(
                "This pay period is not confirmed — only frozen entries can be "
                "backfilled (open periods are recalculated by preview)"
            )

        entries = (
            (
                await db.execute(
                    select(PayrollEntry)
                    .where(PayrollEntry.pay_period_id == period.id)
                    .order_by(
                        PayrollEntry.member_name.asc(), PayrollEntry.revision.asc()
                    )
                )
            )
            .scalars()
            .all()
        )

        result: dict = {
            "period_id": str(period.id),
            "start_date": period.start_date.isoformat(),
            "end_date": period.end_date.isoformat(),
            "updated": 0,
            "unchanged": 0,
            "skipped": [],
        }
        pending = [
            entry
            for entry in entries
            if needs_backfill(entry.breakdown, parse_frozen_breakdown(entry.breakdown))
        ]
        result["unchanged"] = len(entries) - len(pending)
        if not pending:
            return result

        # 읽기 전용 재계산 — 동결 이벤트 불변 (mutate_events=False)
        rows = await payroll_calc_service.preview_period(
            db, period, mutate_events=False
        )
        rows_by_user = {row.user_id: row for row in rows}

        for entry in pending:
            frozen = parse_frozen_breakdown(entry.breakdown)
            if entry.user_id is None:
                self._skip(
                    result, entry,
                    "entry has no user link (account removed) — cannot recompute",
                )
                continue
            row = rows_by_user.get(entry.user_id)
            if row is None:
                self._skip(
                    result, entry,
                    "no recomputed row for this employee — attendance for the "
                    "period is gone or moved to another store",
                )
                continue
            reason = mismatch_reason(entry, frozen, row)
            if reason is not None:
                self._skip(result, entry, reason)
                continue

            entry.breakdown = patched_breakdown(entry.breakdown, row)
            result["updated"] += 1

        await db.flush()
        return result

    async def backfill_all_confirmed(
        self, db: AsyncSession, *, store_id: Optional[UUID] = None
    ) -> list[dict]:
        """확정된 모든 기간을 훑어 백필 (store 한정 가능) — 기간별 결과 목록.

        store 한정 시 그 매장의 레거시 기간 + 매장이 속한 그룹의 group 기간을
        함께 훑는다 (group 스코프 전환 후 신규 기간은 그룹 행이다).
        """
        query = select(PayPeriod).where(PayPeriod.status == "confirmed")
        if store_id is not None:
            from sqlalchemy import or_

            from app.models.organization import Store

            group_id = await db.scalar(
                select(Store.group_id).where(Store.id == store_id)
            )
            conditions = [PayPeriod.store_id == store_id]
            if group_id is not None:
                conditions.append(PayPeriod.store_group_id == group_id)
            query = query.where(or_(*conditions))
        periods = (
            (await db.execute(query.order_by(PayPeriod.start_date.asc())))
            .scalars()
            .all()
        )
        return [
            await self.backfill_frozen_day_amounts(db, period.id)
            for period in periods
        ]

    @staticmethod
    def _skip(result: dict, entry: PayrollEntry, reason: str) -> None:
        result["skipped"].append(
            {
                "user_id": str(entry.user_id) if entry.user_id else None,
                "member_name": entry.member_name,
                "reason": reason,
            }
        )


payroll_backfill_service: PayrollBackfillService = PayrollBackfillService()


__all__ = [
    "AMOUNT_KEYS",
    "CONTEXT_KEY",
    "DAY_WINDOW_KEYS",
    "PayrollBackfillService",
    "has_day_amounts",
    "mismatch_reason",
    "needs_backfill",
    "patched_breakdown",
    "payroll_backfill_service",
]
