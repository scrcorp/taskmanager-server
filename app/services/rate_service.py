"""Rate Service — 개인 시급 단일 진실 원천 (Payroll v1 Phase 1).

시급 읽기/쓰기의 유일한 경로 (스펙: docs/99_inbox/2026-08-03 payroll-v1-스키마-스펙.md §1):

    - rate_at(): 급여/스케줄이 읽는 3단 resolver
        ① effective_date ≤ 기준일 인 최신 hourly_rate_history
           (정렬: effective_date DESC, created_at DESC)
        ② org_members.hourly_rate
        ③ store.default_hourly_rate → org.default_hourly_rate
    - record_rate_change(): 모든 개인 시급 변경의 단일 쓰기 경로
        (이력 insert/같은날 update + org_members/users dual-write
         + schedules 표시 rate 일괄 갱신)
    - apply_due_rate_changes(): 미래 적용일이 도래한 변경을 반영하는 일일 잡
        (APScheduler 진입점: run_rate_change_daily_tick)

전환기(R6) 규칙:
    - canonical = org_members.hourly_rate. users.hourly_rate 는 호환용 미러(dual-write).
    - org_member 행이 아예 없는 계정(레거시/시드)은 users.hourly_rate 로 fallback.
      단, org_member 가 존재하면 그 값(NULL 포함)이 진실 — users 로 fallback 하지 않는다.
    - schedules.hourly_rate 는 표시/예상비용 용도로 격하 — rate 변경 시
      operating_day ≥ 적용일 스케줄을 일괄 갱신 (pay-period lock 이 아직 없어
      모든 status 대상. 표시 일관성 목적).

트랜잭션 규칙:
    - rate_at / person_rate_at / record_rate_change 는 commit 하지 않는다 —
      호출자(서비스)의 트랜잭션이 소유.
    - apply_due_rate_changes 는 cron 헬퍼 관례(attendance_cron_service)에 맞춰
      변경이 있으면 스스로 commit 한다.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org_member import OrgMember, OrgMemberStore
from app.models.organization import Organization, Store
from app.models.rate import BonusRateHistory, HourlyRateHistory
from app.models.schedule import Schedule
from app.models.user import User
from app.utils.exceptions import BadRequestError, NotFoundError

logger = logging.getLogger("uvicorn.error")

_CENT = Decimal("0.01")


def _utc_today() -> date:
    """기준일 = UTC 오늘. 이력 적용/판정의 단일 기준 (org 별 tz 미사용 — 단순성)."""
    return datetime.now(timezone.utc).date()


def _as_money(value) -> Decimal:
    """float/str/Decimal → Numeric(10,2) 정밀도의 Decimal."""
    return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)


class RateService:
    """개인 시급 resolve/변경 서비스 — hourly_rate 의 모든 경로가 여길 지난다."""

    # ── member 식별 ──────────────────────────────────────────────

    async def get_member(
        self,
        db: AsyncSession,
        *,
        org_member: OrgMember | None = None,
        user_id: UUID | None = None,
        organization_id: UUID | None = None,
    ) -> OrgMember | None:
        """org_member 행 또는 (user_id, organization_id) 로 멤버 조회.

        Returns:
            OrgMember | None: 소속 행. (user, org) 쌍에 소속이 없으면 None.
        """
        if org_member is not None:
            return org_member
        if user_id is None or organization_id is None:
            return None
        return await db.scalar(
            select(OrgMember).where(
                OrgMember.user_id == user_id,
                OrgMember.organization_id == organization_id,
            )
        )

    # ── 읽기: person-level ───────────────────────────────────────

    async def _history_rate(
        self, db: AsyncSession, org_member_id: UUID, on_date: date
    ) -> Decimal | None:
        """resolver ①단 — effective_date ≤ on_date 인 최신 이력의 new_rate."""
        rate = await db.scalar(
            select(HourlyRateHistory.new_rate)
            .where(
                HourlyRateHistory.org_member_id == org_member_id,
                HourlyRateHistory.effective_date <= on_date,
            )
            .order_by(
                HourlyRateHistory.effective_date.desc(),
                HourlyRateHistory.created_at.desc(),
            )
            .limit(1)
        )
        return Decimal(rate) if rate is not None else None

    async def person_rate_at(
        self,
        db: AsyncSession,
        org_member: OrgMember | None = None,
        *,
        user_id: UUID | None = None,
        organization_id: UUID | None = None,
        on_date: date | None = None,
    ) -> Decimal | None:
        """개인 rate 만 resolve (①이력 → ②org_members.hourly_rate). 매장/조직 default 제외.

        전환기 fallback: org_member 행이 아예 없으면 users.hourly_rate.
        (행이 있는데 NULL 이면 '개인 rate 미지정' — fallback 하지 않는다.)
        """
        if org_member is None and user_id is None:
            raise ValueError("person_rate_at requires org_member or user_id")
        resolved_date = on_date or _utc_today()

        member = await self.get_member(
            db, org_member=org_member, user_id=user_id, organization_id=organization_id
        )
        if member is None:
            # org_member 미생성 계정 (레거시) — users.hourly_rate fallback
            if user_id is not None:
                legacy = await db.scalar(
                    select(User.hourly_rate).where(User.id == user_id)
                )
                return Decimal(legacy) if legacy is not None else None
            return None

        hist = await self._history_rate(db, member.id, resolved_date)
        if hist is not None:
            return hist
        return Decimal(member.hourly_rate) if member.hourly_rate is not None else None

    async def person_rates_map(
        self,
        db: AsyncSession,
        pairs: set[tuple[UUID, UUID]],
        on_date: date | None = None,
    ) -> dict[tuple[UUID, UUID], Decimal]:
        """(user_id, organization_id) 쌍 → 개인 rate 일괄 resolve (목록 경로 N+1 방지).

        rate 가 없는 쌍은 결과에서 빠진다 (호출측에서 store/org default cascade).
        """
        if not pairs:
            return {}
        resolved_date = on_date or _utc_today()
        user_ids = {p[0] for p in pairs}
        org_ids = {p[1] for p in pairs}

        members = [
            m
            for m in (
                await db.execute(
                    select(OrgMember).where(
                        OrgMember.user_id.in_(user_ids),
                        OrgMember.organization_id.in_(org_ids),
                    )
                )
            ).scalars()
            if (m.user_id, m.organization_id) in pairs
        ]

        # 멤버별 최신 due 이력 1건 — row_number 윈도우로 테이블당 1쿼리
        hist_by_member: dict[UUID, Decimal] = {}
        if members:
            rn = (
                func.row_number()
                .over(
                    partition_by=HourlyRateHistory.org_member_id,
                    order_by=(
                        HourlyRateHistory.effective_date.desc(),
                        HourlyRateHistory.created_at.desc(),
                    ),
                )
                .label("rn")
            )
            sub = (
                select(
                    HourlyRateHistory.org_member_id,
                    HourlyRateHistory.new_rate,
                    rn,
                )
                .where(
                    HourlyRateHistory.org_member_id.in_([m.id for m in members]),
                    HourlyRateHistory.effective_date <= resolved_date,
                )
                .subquery()
            )
            rows = await db.execute(
                select(sub.c.org_member_id, sub.c.new_rate).where(sub.c.rn == 1)
            )
            hist_by_member = {r.org_member_id: Decimal(r.new_rate) for r in rows}

        result: dict[tuple[UUID, UUID], Decimal] = {}
        member_pairs: set[tuple[UUID, UUID]] = set()
        for m in members:
            member_pairs.add((m.user_id, m.organization_id))
            rate = hist_by_member.get(m.id)
            if rate is None and m.hourly_rate is not None:
                rate = Decimal(m.hourly_rate)
            if rate is not None:
                result[(m.user_id, m.organization_id)] = rate

        # 전환기 fallback — 멤버 행이 없는 쌍만 users.hourly_rate
        missing = pairs - member_pairs
        if missing:
            legacy_rows = await db.execute(
                select(User.id, User.hourly_rate).where(
                    User.id.in_({p[0] for p in missing}),
                    User.hourly_rate.isnot(None),
                )
            )
            legacy = {r.id: Decimal(r.hourly_rate) for r in legacy_rows}
            for pair in missing:
                if pair[0] in legacy:
                    result[pair] = legacy[pair[0]]
        return result

    # ── 읽기: full cascade ───────────────────────────────────────

    async def rate_at(
        self,
        db: AsyncSession,
        org_member: OrgMember | None = None,
        store_id: UUID | None = None,
        on_date: date | None = None,
        *,
        user_id: UUID | None = None,
        organization_id: UUID | None = None,
    ) -> Decimal | None:
        """시급 3단 resolver — 급여 계산은 이 함수만 읽는다.

        ① effective_date ≤ on_date 최신 이력 ② org_members.hourly_rate
        ③ store.default_hourly_rate → org.default_hourly_rate

        Args:
            org_member: 소속 행 (있으면 user_id/organization_id 불필요)
            store_id: ③단 매장 default 용 (없으면 매장 단계 skip)
            on_date: 기준일 (기본: UTC 오늘)
            user_id/organization_id: org_member 대신 쓸 식별자.
                organization_id 가 없으면 store 로부터 유도.

        Returns:
            Decimal | None: resolve 된 시급. 어디에도 없으면 None (rate=0 게이트는
                호출측 판정 — org default 가 0 이면 Decimal('0.00') 이 반환된다).
        """
        resolved_date = on_date or _utc_today()

        org_id = organization_id
        if org_member is not None:
            org_id = org_id or org_member.organization_id
        if org_id is None and store_id is not None:
            org_id = await db.scalar(
                select(Store.organization_id).where(Store.id == store_id)
            )

        # ①/② person-level
        if org_member is not None or user_id is not None:
            person = await self.person_rate_at(
                db,
                org_member,
                user_id=user_id,
                organization_id=org_id,
                on_date=resolved_date,
            )
            if person is not None:
                return person

        # ③ store → org default
        if store_id is not None:
            store_rate = await db.scalar(
                select(Store.default_hourly_rate).where(Store.id == store_id)
            )
            if store_rate is not None:
                return Decimal(store_rate)
        if org_id is not None:
            org_rate = await db.scalar(
                select(Organization.default_hourly_rate).where(
                    Organization.id == org_id
                )
            )
            if org_rate is not None:
                return Decimal(org_rate)
        return None

    # ── 쓰기: 단일 mutation 경로 ─────────────────────────────────

    async def _apply_member_rate(
        self,
        db: AsyncSession,
        member: OrgMember,
        rate: Decimal,
        effective_date: date,
    ) -> None:
        """적용일이 도래한 rate 를 실제 반영.

        org_members(canonical) + users(전환기 미러) + schedules(표시용,
        operating_day ≥ 적용일, 전 status — pay-period lock 부재) 3곳 동시 갱신.
        """
        member.hourly_rate = rate
        await db.execute(
            update(User).where(User.id == member.user_id).values(hourly_rate=rate)
        )
        await db.execute(
            update(Schedule)
            .where(
                Schedule.user_id == member.user_id,
                Schedule.organization_id == member.organization_id,
                Schedule.operating_day >= effective_date,
            )
            .values(hourly_rate=rate)
        )
        await db.flush()

    async def record_rate_change(
        self,
        db: AsyncSession,
        *,
        org_member: OrgMember | None = None,
        user_id: UUID | None = None,
        organization_id: UUID | None = None,
        new_rate,
        effective_date: date | None = None,
        reason: str | None = None,
        changed_by: UUID | None = None,
    ) -> HourlyRateHistory | None:
        """개인 시급 변경의 단일 쓰기 경로 — 이력 + dual-write + 스케줄 갱신.

        규칙:
            - new_rate > 0 필수 (0/음수는 400 — DB CHECK 와 동일 규칙 선검증)
            - 같은 (멤버, effective_date) 재등록은 기존 행 update
              (old_rate 는 원본 보존, new_rate/reason/changed_by 만 갱신)
            - 신규 행 old_rate = person_rate_at(적용일 전날) — 개인 rate 이력/컬럼
              어디에도 없으면 NULL (= 최초 기록)
            - effective_date ≤ 오늘(UTC): org_members + users 미러 + 해당 멤버의
              operating_day ≥ 적용일 스케줄 hourly_rate 즉시 갱신
            - 미래 적용일: 이력만 기록 (반영은 apply_due_rate_changes 일일 잡)
            - 현재 개인 rate 와 동일한 값 재등록은 no-op (이력 노이즈 방지) —
              단 컬럼 drift 가 있으면 동기화만 수행

        commit 하지 않는다 — 호출자 트랜잭션 소유.

        Returns:
            HourlyRateHistory | None: 기록/갱신된 이력 행. no-op 이면 None.

        Raises:
            BadRequestError: new_rate ≤ 0
            NotFoundError: (user, org) 에 org_member 소속이 없을 때
        """
        member = await self.get_member(
            db, org_member=org_member, user_id=user_id, organization_id=organization_id
        )
        if member is None:
            raise NotFoundError("Organization member not found for rate change")

        rate = _as_money(new_rate)
        if rate <= 0:
            raise BadRequestError("Hourly rate must be greater than 0")

        today = _utc_today()
        eff = effective_date or today

        existing = await db.scalar(
            select(HourlyRateHistory).where(
                HourlyRateHistory.org_member_id == member.id,
                HourlyRateHistory.effective_date == eff,
            )
        )

        if existing is None:
            current = await self.person_rate_at(db, member, on_date=eff)
            if current is not None and current == rate:
                # 값 변화 없음 — 이력 노이즈 방지. 컬럼 drift 만 자가 치유.
                if eff <= today and (
                    member.hourly_rate is None or Decimal(member.hourly_rate) != rate
                ):
                    await self._apply_member_rate(db, member, rate, eff)
                return None
            # old_rate = 적용일 전날의 개인 rate (이력 → member 컬럼).
            # 개인 rate 가 전혀 없었으면 NULL = 최초 기록.
            old = await self.person_rate_at(
                db, member, on_date=eff - timedelta(days=1)
            )
            row = HourlyRateHistory(
                organization_id=member.organization_id,
                org_member_id=member.id,
                old_rate=old,
                new_rate=rate,
                effective_date=eff,
                reason=reason,
                changed_by=changed_by,
            )
            db.add(row)
        else:
            # 같은 날 재등록 — 기존 행 update. old_rate/created_at 은 원본 보존.
            existing.new_rate = rate
            existing.reason = reason
            existing.changed_by = changed_by
            row = existing

        await db.flush()

        if eff <= today:
            await self._apply_member_rate(db, member, rate, eff)
        return row

    # ── 일일 잡: 도래한 미래 변경 반영 ───────────────────────────

    async def apply_due_rate_changes(self, db: AsyncSession) -> int:
        """적용일이 도래했는데 아직 컬럼에 반영 안 된 변경을 일괄 반영 (멱등).

        멤버별 '최신 due 이력'(effective_date ≤ 오늘, 정렬 규칙 동일) 1건을 찾아
        org_members.hourly_rate 와 다르면 _apply_member_rate 로 반영한다.
        반영 후엔 값이 일치하므로 재실행해도 no-op — 멱등.

        변경이 있으면 commit (cron 헬퍼 관례).

        Returns:
            int: 반영된 멤버 수
        """
        today = _utc_today()
        rn = (
            func.row_number()
            .over(
                partition_by=HourlyRateHistory.org_member_id,
                order_by=(
                    HourlyRateHistory.effective_date.desc(),
                    HourlyRateHistory.created_at.desc(),
                ),
            )
            .label("rn")
        )
        sub = (
            select(
                HourlyRateHistory.org_member_id,
                HourlyRateHistory.new_rate,
                HourlyRateHistory.effective_date,
                rn,
            )
            .where(HourlyRateHistory.effective_date <= today)
            .subquery()
        )
        rows = await db.execute(
            select(OrgMember, sub.c.new_rate, sub.c.effective_date)
            .join(sub, sub.c.org_member_id == OrgMember.id)
            .where(
                sub.c.rn == 1,
                or_(
                    OrgMember.hourly_rate.is_(None),
                    OrgMember.hourly_rate != sub.c.new_rate,
                ),
            )
        )
        applied = 0
        for member, new_rate, eff in rows.all():
            await self._apply_member_rate(db, member, Decimal(new_rate), eff)
            applied += 1
        if applied:
            await db.commit()
        return applied

    # ── 성과 보너스 가산율 ────────────────────────────────────────────

    async def bonus_rate_at(
        self,
        db: AsyncSession,
        member_store_id: UUID,
        on_date: date,
    ) -> Decimal:
        """보너스 가산율 2단 resolver — 급여 계산은 이 함수만 읽는다.

        ① effective_date ≤ on_date 인 최신 bonus_rate_history
        ② org_member_stores.bonus_rate

        시급(rate_at)과 달리 store/org default 단계가 없다 — 보너스는 개인별로만
        존재하는 값이라 폴백할 상위 기본값이라는 개념이 없기 때문이다.

        Returns:
            Decimal: 가산율. 이력도 컬럼도 없으면 Decimal("0") (보너스 없음).
        """
        history = await db.scalar(
            select(BonusRateHistory.new_rate)
            .where(
                BonusRateHistory.org_member_store_id == member_store_id,
                BonusRateHistory.effective_date <= on_date,
            )
            .order_by(
                BonusRateHistory.effective_date.desc(),
                BonusRateHistory.created_at.desc(),
            )
            .limit(1)
        )
        if history is not None:
            return Decimal(history)
        current = await db.scalar(
            select(OrgMemberStore.bonus_rate).where(OrgMemberStore.id == member_store_id)
        )
        return Decimal(current) if current is not None else Decimal("0")

    async def record_bonus_rate_change(
        self,
        db: AsyncSession,
        member_store: OrgMemberStore,
        new_rate: Decimal,
        effective_date: date,
        *,
        organization_id: UUID,
        reason: str | None = None,
        changed_by: UUID | None = None,
    ) -> BonusRateHistory:
        """보너스율 변경의 단일 쓰기 경로 — 이력 + 현재값을 함께 갱신한다.

        같은 (member_store, effective_date) 재등록은 insert 가 아니라 기존 행 update
        (시급 이력과 동일 규칙). effective_date 가 급여기간 시작일인지의 검증은
        호출측(API 계층)이 한다 — 시딩/백필처럼 우회가 필요한 경로가 있기 때문.
        """
        existing = await db.scalar(
            select(BonusRateHistory).where(
                BonusRateHistory.org_member_store_id == member_store.id,
                BonusRateHistory.effective_date == effective_date,
            )
        )
        old_rate = member_store.bonus_rate
        if existing is not None:
            existing.new_rate = new_rate
            existing.reason = reason
            existing.changed_by = changed_by
            row = existing
        else:
            row = BonusRateHistory(
                organization_id=organization_id,
                org_member_store_id=member_store.id,
                old_rate=old_rate,
                new_rate=new_rate,
                effective_date=effective_date,
                reason=reason,
                changed_by=changed_by,
            )
            db.add(row)

        # 적용일이 이미 도래했으면 현재값도 즉시 반영. 미래 적용이면 이력만 남기고
        # apply_due_bonus_changes() 가 그날 반영한다 (시급 변경과 같은 패턴).
        if effective_date <= _utc_today():
            member_store.bonus_rate = new_rate
        return row

    async def apply_due_bonus_changes(self, db: AsyncSession) -> int:
        """적용일이 도래한 보너스율 변경을 현재값에 반영 (일일 잡)."""
        today = _utc_today()
        rows = await db.execute(
            select(BonusRateHistory, OrgMemberStore)
            .join(
                OrgMemberStore,
                OrgMemberStore.id == BonusRateHistory.org_member_store_id,
            )
            .where(
                BonusRateHistory.effective_date <= today,
                or_(
                    OrgMemberStore.bonus_rate.is_(None),
                    OrgMemberStore.bonus_rate != BonusRateHistory.new_rate,
                ),
            )
            .order_by(BonusRateHistory.effective_date.desc())
        )
        seen: set[UUID] = set()
        applied = 0
        for history, member_store in rows.all():
            if member_store.id in seen:
                continue  # 같은 배정의 더 오래된 이력은 건너뛴다 (최신이 이긴다)
            seen.add(member_store.id)
            member_store.bonus_rate = history.new_rate
            applied += 1
        if applied:
            await db.commit()
        return applied


# 싱글턴 인스턴스 — Singleton instance
rate_service: RateService = RateService()


async def run_rate_change_daily_tick() -> None:
    """APScheduler 진입점 — 매일 1회 (UTC 자정 직후) 도래한 rate 변경 반영."""
    from app.database import async_session

    try:
        async with async_session() as db:
            applied = await rate_service.apply_due_rate_changes(db)
            if applied:
                logger.info(f"[rate_cron] applied {applied} due rate change(s)")
    except Exception as e:
        logger.warning(f"[rate_cron] tick failed: {e}")
