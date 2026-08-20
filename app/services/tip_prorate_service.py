"""Tip Prorate Service — 근무시간 비례 팁 자동 분배 (분배 방식 2안).

매장 설정 `payroll.tip_distribution_mode = "hours_prorated"` 인 매장에서,
그날 걷힌 팁을 **그날 근무한 대상 직원들에게 근무시간 비례로** 나눈다.

규칙 (docs/99_inbox/2026-08-11-payroll-cfs-export-결정사항.md §4-2):
    - 대상: 그 영업일 근무 기록이 있고 org_member_stores.tip_eligible = True 인 직원
    - 가중치: min(그날 net 근무분, 480분=8시간)
    - 주기: 일 단위 (그날그날)
    - 수락 절차 없음 — 규칙 산출값이라 자동 확정
    - 카드/현금 풀을 **같은 가중치로 각각** 나눈다

왜 카드/현금을 따로 나누는가:
    지급 대상(카드)과 신고 대상(현금)이 갈릴 수 있어서다. 가중치가 동일하므로
    card+cash 합은 언제나 그 사람의 풀 몫과 정확히 같다 — 나눠 담아도 총액은 안 틀어진다.

라운딩:
    금액은 센트 단위로 내림 배분하고, 반올림 손실(잔돈)은 가중치가 가장 큰 사람에게
    몰아준다. 그래야 Σ분배액 == 풀 이 정확히 성립한다 (급여 검증이 이 등식에 의존).
    동점이면 user_id 순으로 결정 — 재계산해도 같은 결과가 나와야 하기 때문.

트랜잭션 규칙:
    - allocate_day / allocate_range 는 commit 하지 않는다 (flush 만). 호출자 소유.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import Attendance
from app.models.org_member import OrgMember, OrgMemberStore
from app.models.organization import Store
from app.models.tip import TipAllocation, TipEntry
from app.seeds.settings_seed import TIP_DISTRIBUTION_MODE_KEY, TIP_MODE_HOURS_PRORATED
from app.utils.settings_resolver import SettingNotRegisteredError, resolve_setting

logger = logging.getLogger("uvicorn.error")

_CENT = Decimal("0.01")

# 분배 가중치 상한 — 하루 8시간. 장시간 근무자가 팁을 독식하는 걸 막는 장치다.
MAX_WEIGHT_MINUTES = 8 * 60


class TipProrateService:
    """근무시간 비례 팁 분배 — 계산과 저장의 단일 경로."""

    async def store_mode(self, db: AsyncSession, store_id: UUID) -> str:
        """매장의 팁 분배 방식. 레지스트리 미시드 DB 는 'none' 으로 본다."""
        org_id = await db.scalar(
            select(Store.organization_id).where(Store.id == store_id)
        )
        if org_id is None:
            return "none"
        try:
            value = await resolve_setting(
                db,
                TIP_DISTRIBUTION_MODE_KEY,
                organization_id=org_id,
                store_id=store_id,
            )
        except SettingNotRegisteredError:
            return "none"
        return str(value or "none")

    async def allocate_day(
        self,
        db: AsyncSession,
        *,
        store_id: UUID,
        work_date: date,
    ) -> list[TipAllocation]:
        """하루치 재계산 — 기존 배분을 지우고 다시 만든다.

        부분 갱신하지 않는 이유: 한 사람의 근무시간만 바뀌어도 그날 전원의 몫이
        바뀌기 때문에, 일 단위 전량 재계산이 유일하게 일관된 방법이다.

        Returns:
            생성된 배분 행 목록 (풀이 0이거나 대상자가 없으면 빈 목록)
        """
        await db.execute(
            delete(TipAllocation).where(
                TipAllocation.store_id == store_id,
                TipAllocation.date == work_date,
            )
        )

        card_pool, cash_pool = await self._pool_for_day(db, store_id, work_date)
        if card_pool <= 0 and cash_pool <= 0:
            return []

        weights = await self._weights_for_day(db, store_id, work_date)
        if not weights:
            # 팁은 걷혔는데 대상자가 없다 — 데이터 이상 신호라 로그로 남긴다.
            # (자동 생성물이므로 예외로 막지는 않는다: 급여 산출 전체가 멈추면 더 나쁘다)
            logger.warning(
                f"[tip_prorate] store={store_id} {work_date}: "
                f"pool card={card_pool} cash={cash_pool} but no eligible worker"
            )
            return []

        card_shares = self._split(card_pool, weights)
        cash_shares = self._split(cash_pool, weights)

        rows: list[TipAllocation] = []
        for user_id, weight in weights.items():
            row = TipAllocation(
                store_id=store_id,
                employee_id=user_id,
                date=work_date,
                card_amount=card_shares[user_id],
                cash_amount=cash_shares[user_id],
                weight_minutes=weight,
            )
            db.add(row)
            rows.append(row)
        await db.flush()
        return rows

    async def allocate_range(
        self,
        db: AsyncSession,
        *,
        store_id: UUID,
        start: date,
        end: date,
    ) -> int:
        """기간 일괄 재계산. hours_prorated 매장이 아니면 아무것도 하지 않는다."""
        if await self.store_mode(db, store_id) != TIP_MODE_HOURS_PRORATED:
            return 0
        days = 0
        cursor = start
        while cursor <= end:
            await self.allocate_day(db, store_id=store_id, work_date=cursor)
            days += 1
            cursor += timedelta(days=1)
        return days

    async def totals_for_period(
        self,
        db: AsyncSession,
        *,
        store_id: UUID,
        start: date,
        end: date,
    ) -> dict[UUID, dict[str, Decimal]]:
        """기간 내 직원별 배분 합계 — {user_id: {"card": x, "cash": y}}."""
        rows = (
            await db.scalars(
                select(TipAllocation).where(
                    TipAllocation.store_id == store_id,
                    TipAllocation.date >= start,
                    TipAllocation.date <= end,
                )
            )
        ).all()
        totals: dict[UUID, dict[str, Decimal]] = {}
        for row in rows:
            bucket = totals.setdefault(
                row.employee_id, {"card": Decimal("0.00"), "cash": Decimal("0.00")}
            )
            bucket["card"] += row.card_amount
            bucket["cash"] += row.cash_amount
        return totals

    # ── 내부 ────────────────────────────────────────────────────────

    async def _pool_for_day(
        self, db: AsyncSession, store_id: UUID, work_date: date
    ) -> tuple[Decimal, Decimal]:
        """그날 그 매장에 걷힌 팁 총액 (카드, 현금).

        누가 입력했는지는 상관없다 — 2안에서 entry 는 "이 매장에 이만큼 걷혔다"는
        기록일 뿐이고, 소유자 개념이 없다.
        """
        entries = (
            await db.scalars(
                select(TipEntry).where(
                    TipEntry.store_id == store_id,
                    TipEntry.date == work_date,
                )
            )
        ).all()
        card = sum((e.card_tips for e in entries), start=Decimal("0.00"))
        cash = sum((e.cash_tips_kept for e in entries), start=Decimal("0.00"))
        return card, cash

    async def _weights_for_day(
        self, db: AsyncSession, store_id: UUID, work_date: date
    ) -> dict[UUID, int]:
        """대상 직원별 가중치 = min(그날 net 근무분, 480). 0분은 제외."""
        from app.services.attendance_service import (
            attendance_service,
            compute_net_work_minutes,
        )

        attendances = (
            await db.scalars(
                select(Attendance).where(
                    Attendance.store_id == store_id,
                    Attendance.work_date == work_date,
                    Attendance.status != "cancelled",
                    Attendance.user_id.is_not(None),
                )
            )
        ).all()
        if not attendances:
            return {}
        breaks_map = await attendance_service._load_breaks_map(
            db, [a.id for a in attendances]
        )

        # 하루에 여러 번 출퇴근했으면 합산 (급여 계산과 같은 C1 net 공식)
        net_by_user: dict[UUID, int] = {}
        for att in attendances:
            net = compute_net_work_minutes(att, breaks_map.get(att.id, []))
            if net is None or net <= 0:
                continue  # 미퇴근/0분 — 가중치 없음
            net_by_user[att.user_id] = net_by_user.get(att.user_id, 0) + net
        if not net_by_user:
            return {}

        eligible = await self._eligible_users(db, store_id, set(net_by_user))
        return {
            user_id: min(minutes, MAX_WEIGHT_MINUTES)
            for user_id, minutes in net_by_user.items()
            if user_id in eligible
        }

    async def _eligible_users(
        self, db: AsyncSession, store_id: UUID, user_ids: set[UUID]
    ) -> set[UUID]:
        """org_member_stores.tip_eligible = True 인 직원만."""
        if not user_ids:
            return set()
        rows = await db.execute(
            select(OrgMember.user_id)
            .join(OrgMemberStore, OrgMemberStore.org_member_id == OrgMember.id)
            .where(
                OrgMemberStore.store_id == store_id,
                OrgMemberStore.tip_eligible.is_(True),
                OrgMember.user_id.in_(user_ids),
            )
        )
        return {r.user_id for r in rows}

    @staticmethod
    def _split(pool: Decimal, weights: dict[UUID, int]) -> dict[UUID, Decimal]:
        """가중치 비례 분배 — 센트 내림 후 잔돈은 최대 가중치자에게.

        Σ결과 == pool 이 정확히 성립해야 한다 (급여 검증이 이 등식에 의존).
        동점 시 user_id 순으로 정해 재계산 안정성을 보장한다.
        """
        total_weight = sum(weights.values())
        if pool <= 0 or total_weight <= 0:
            return {user_id: Decimal("0.00") for user_id in weights}

        shares: dict[UUID, Decimal] = {}
        allocated = Decimal("0.00")
        for user_id, weight in weights.items():
            share = (pool * Decimal(weight) / Decimal(total_weight)).quantize(
                _CENT, rounding=ROUND_DOWN
            )
            shares[user_id] = share
            allocated += share

        remainder = pool - allocated
        if remainder > 0:
            top = max(weights.items(), key=lambda kv: (kv[1], str(kv[0])))[0]
            shares[top] += remainder
        return shares


# 싱글턴 인스턴스 — Singleton instance
tip_prorate_service: TipProrateService = TipProrateService()
