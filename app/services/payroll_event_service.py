"""Payroll Event Service — penalty 감지 + payroll_events 라이프사이클 (Phase 2).

payroll_events(스펙 §6, grain = 일 단위)의 유일한 쓰기 경로:

    - detect_and_upsert_events(): meal/rest penalty 를 attendance 원천에서 재감지
      → (org, user, work_date, kind) 일 키로 upsert
    - upsert_classification_events(): 계산 엔진의 일별 분류 출력(daily_ot /
      weekly_ot / seventh_day)을 같은 upsert 의미론으로 반영
    - list_events() / tag_event(): 콘솔 조회 + 귀책 태그(E1)

감지 규칙 (v1, 상수는 app/core/payroll_rules.py):
    - meal_penalty: 일 C1 net > 5h 인데 그날 attendance 전체에 30분 이상
      완료된 무급 meal break 세션이 하나도 없으면 위반 (presence 기반).
      타이밍 기반 고도화 — "5th hour 종료 전에 meal 시작했는지" 판정 — 는 v2.
    - rest_penalty: 일 net ≥ 3.5h 부터 required_rest_breaks() 단계
      (3.5–6h→1, >6–10h→2, >10h→3)만큼 유급 10분 휴게 세션이 필요.
      기록된 완료 세션 수 < 필요 수면 위반 (세션 길이는 v1 미검증 — presence 기반).
    - kind 당 하루 1건 — uq_payroll_event_user_date_kind 가 스키마 보장.

upsert 의미론 (재감지 마다):
    - 신규 조건 → INSERT
    - 기존 행 + 조건 유지 → reason/attendance_id 갱신.
      attribution/tagged_by/tagged_at 은 **보존** (E1 태깅은 재감지에 불변).
      voided 상태였으면 조건 재발이므로 voided_at 해제(revive).
    - 조건 소멸 → 삭제 대신 voided_at 마킹 (태깅 보존, 집계는 voided IS NULL 만)
    - pay_period_id 가 있는 행(=confirm 동결) 은 어떤 변경도 하지 않음 (amendment 전용)
    - cancelled attendance 는 입력에서 제외
    - 그날 net 을 하나도 계산할 수 없으면(전부 미퇴근) 판정 보류 —
      기존 이벤트를 건드리지 않는다 (마감 게이트 ② 가 열린 근무를 잡는다)

reason 은 콘솔/명세서에 그대로 노출되는 사람이 읽는 문구 — 영어 (UI 텍스트 규칙).

v1 known limitation: 감지는 store 단위 스캔이라 같은 org 의 두 매장에서 같은 날
근무한 경우 일 합산이 매장별로 따로 평가된다 (일 키는 org grain — 나중에 감지한
매장이 reason 을 덮어씀). v1 은 단일 매장 org 전제(설계방향 §1)라 실질 영향 없음.

트랜잭션 규칙 (rate_service 관례):
    - detect_and_upsert_events / upsert_classification_events 는 commit 하지
      않는다 (flush 만) — 재계산 훅/계산 엔진 호출자의 트랜잭션이 소유.
    - tag_event 는 단독 API 액션이라 스스로 commit.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.payroll_rules import (
    ATTRIBUTION_MANAGEMENT,
    ATTRIBUTION_STAFF,
    CA_DAILY_DT_HOURS,
    CA_DAILY_OT_HOURS,
    CLASSIFICATION_EVENT_KINDS,
    EVENT_KIND_DAILY_OT,
    EVENT_KIND_MEAL_PENALTY,
    EVENT_KIND_REST_PENALTY,
    EVENT_KIND_SEVENTH_DAY,
    EVENT_KIND_WEEKLY_OT,
    MEAL_BREAK_MIN_MINUTES,
    MEAL_PENALTY_NET_MINUTES,
    PENALTY_EVENT_KINDS,
    VALID_ATTRIBUTIONS,
    WEEKLY_OT_HOURS,
    required_rest_breaks,
)
from app.models.attendance import Attendance
from app.models.attendance_break import (
    PAID_BREAK_TYPES,
    UNPAID_BREAK_TYPES,
    AttendanceBreak,
)
from app.models.organization import Store
from app.models.payroll import PayrollEvent
from app.utils.exceptions import BadRequestError, NotFoundError


def _fmt_hours(minutes: int) -> str:
    """분 → 'H.Th' 표기 (reason 문구용). 372 → '6.2'."""
    return f"{minutes / 60:.1f}"


# ---------------------------------------------------------------------------
# 순수 규칙 평가 — DB 없음 (unit test 대상)
# ---------------------------------------------------------------------------


def evaluate_meal_penalty(
    day_net_minutes: int,
    day_breaks: list[AttendanceBreak],
) -> str | None:
    """meal penalty 판정 — 위반이면 reason(영어), 아니면 None.

    조건: 일 net > 5h AND 그날 완료된(ended_at 有) 무급 meal 세션 중
    30분 이상이 하나도 없음. 진행 중 세션은 duration 미확정이라 불인정.
    레거시 break_type(unpaid_long)도 무급 meal 로 인식 (dual-read).
    """
    if day_net_minutes <= MEAL_PENALTY_NET_MINUTES:
        return None
    for br in day_breaks:
        if (
            br.break_type in UNPAID_BREAK_TYPES
            and br.ended_at is not None
            and (br.duration_minutes or 0) >= MEAL_BREAK_MIN_MINUTES
        ):
            return None
    return (
        f"Worked {_fmt_hours(day_net_minutes)}h with no "
        f"{MEAL_BREAK_MIN_MINUTES}-min meal break"
    )


def evaluate_rest_penalty(
    day_net_minutes: int,
    day_breaks: list[AttendanceBreak],
) -> str | None:
    """rest penalty 판정 — 위반이면 reason(영어), 아니면 None.

    조건: required_rest_breaks(net) > 그날 완료된 유급 세션 수.
    세션 길이는 v1 미검증 (presence 기반). 레거시 paid_short 도 인식.
    """
    required = required_rest_breaks(day_net_minutes)
    if required == 0:
        return None
    taken = sum(
        1
        for br in day_breaks
        if br.break_type in PAID_BREAK_TYPES and br.ended_at is not None
    )
    if taken >= required:
        return None
    return (
        f"Worked {_fmt_hours(day_net_minutes)}h with {taken} of "
        f"{required} required 10-min rest break(s)"
    )


# ---------------------------------------------------------------------------
# 계산 엔진 → 분류 이벤트 입력 계약
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassifiedDay:
    """계산 엔진의 일별 분류 출력 1건 — upsert_classification_events 입력 계약.

    엔진이 (user, work_date) 별로 하나씩 만들어 넘긴다. 어떤 kind 도 발생하지
    않은 날은 목록에 넣지 않아도 된다 (넘겨도 0/False 면 동일하게 처리).
    목록은 (store, date_range) 에 대해 **authoritative** — 범위 내 기존 분류
    이벤트 중 목록이 재현하지 않는 것은 조건 소멸로 보고 void 한다.

    Attributes:
        user_id / work_date: 일 키
        daily_ot_minutes: 일 8h 초과분 (1.5x 구간, C2)
        daily_dt_minutes: 일 12h 초과분 (2x 구간, C2) — daily_ot kind 에 합산 표기
        weekly_ot_minutes: 이 날에 귀속된 주 40h 초과분 (C2/C4)
        seventh_day: 7일 연속 근무의 7일째 여부 (C2)
        attendance_id: 그날 attendance 가 1:1 이면 해당 id, 아니면 None (참고용)
    """

    user_id: UUID
    work_date: date
    daily_ot_minutes: int = 0
    daily_dt_minutes: int = 0
    weekly_ot_minutes: int = 0
    seventh_day: bool = False
    attendance_id: UUID | None = None


@dataclass
class EventUpsertSummary:
    """upsert 1회 실행 결과 카운터 (테스트/로깅용)."""

    created: int = 0
    updated: int = 0
    revived: int = 0
    voided: int = 0
    skipped_frozen: int = 0
    event_ids: list[UUID] = field(default_factory=list)


class PayrollEventService:
    """payroll_events 감지/upsert/조회/태깅 서비스."""

    # ── 내부 헬퍼 ────────────────────────────────────────────────

    async def _resolve_org_id(self, db: AsyncSession, store_id: UUID) -> UUID:
        org_id = await db.scalar(
            select(Store.organization_id).where(Store.id == store_id)
        )
        if org_id is None:
            raise NotFoundError("Store not found")
        return org_id

    async def _upsert(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        store_id: UUID,
        start_date: date,
        end_date: date,
        kinds: frozenset[str],
        detected: dict[tuple[UUID, date, str], dict],
        hold_keys: set[tuple[UUID, date]],
    ) -> EventUpsertSummary:
        """일 키 upsert 공통 코어 — penalty/classification 경로 공유.

        Args:
            kinds: 이 실행이 소유하는 kind 집합 (다른 kind 는 안 건드림)
            detected: {(user_id, work_date, kind): {"reason": str,
                "attendance_id": UUID | None}} — 현재 조건이 성립하는 키
            hold_keys: 판정 보류 (user_id, work_date) — 해당 일의 기존 이벤트를
                void 하지 않는다 (net 미계산 일 등)

        의미론: INSERT 신규 / UPDATE reason·attendance_id (attribution 계열 보존,
        voided 면 revive) / detected 에 없으면 voided_at 마킹 / frozen 은 불변.
        void 는 이 store 가 만든 행(row.store_id == store_id)만 대상 — 같은 org
        다른 매장 run 이 소유한 행을 이 매장 스캔이 끄지 않도록.

        commit 하지 않는다 — 호출자 트랜잭션 소유.
        """
        # 기존 행은 org 범위로 로드 — 일 키 unique 가 org grain 이라, 다른 매장이
        # 먼저 만든 행과의 INSERT 충돌을 upsert 로 흡수하기 위해.
        existing_rows = (
            (
                await db.execute(
                    select(PayrollEvent).where(
                        PayrollEvent.organization_id == organization_id,
                        PayrollEvent.kind.in_(kinds),
                        PayrollEvent.work_date >= start_date,
                        PayrollEvent.work_date <= end_date,
                        PayrollEvent.user_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        existing: dict[tuple[UUID, date, str], PayrollEvent] = {
            (row.user_id, row.work_date, row.kind): row for row in existing_rows
        }

        now = datetime.now(timezone.utc)
        summary = EventUpsertSummary()
        touched: list[PayrollEvent] = []

        # 성립 조건 반영 — INSERT / UPDATE(+revive)
        for key, payload in detected.items():
            row = existing.get(key)
            if row is None:
                user_id, work_date, kind = key
                row = PayrollEvent(
                    organization_id=organization_id,
                    store_id=store_id,
                    user_id=user_id,
                    attendance_id=payload.get("attendance_id"),
                    work_date=work_date,
                    kind=kind,
                    reason=payload["reason"],
                )
                db.add(row)
                touched.append(row)
                summary.created += 1
                continue
            if row.pay_period_id is not None:
                # confirm 동결 — amendment 에서만 변경
                summary.skipped_frozen += 1
                continue
            row.reason = payload["reason"]
            row.attendance_id = payload.get("attendance_id")
            if row.voided_at is not None:
                # 조건 재발 — revive. attribution/tagged_by/tagged_at 은 그대로.
                row.voided_at = None
                summary.revived += 1
            else:
                summary.updated += 1
            touched.append(row)

        # 조건 소멸 반영 — voided_at 마킹 (삭제 금지)
        for key, row in existing.items():
            if key in detected:
                continue
            if (key[0], key[1]) in hold_keys:
                continue  # 판정 보류 일 — 건드리지 않음
            if row.store_id != store_id:
                continue  # 다른 매장 run 소유 행
            if row.pay_period_id is not None:
                summary.skipped_frozen += 1
                continue
            if row.voided_at is None:
                row.voided_at = now
                summary.voided += 1

        await db.flush()
        summary.event_ids = [row.id for row in touched]
        return summary

    # ── penalty 감지 ─────────────────────────────────────────────

    async def detect_and_upsert_events(
        self,
        db: AsyncSession,
        store_id: UUID,
        start_date: date,
        end_date: date,
    ) -> EventUpsertSummary:
        """범위 내 meal/rest penalty 를 attendance 원천에서 재감지해 upsert.

        입력: 해당 store 의 non-cancelled attendance (user 유실 행 제외).
        일 net 은 attendance 별 C1 공식(compute_net_work_minutes)의 합 —
        net 계산 가능한(퇴근 완료) attendance 만 합산하고, 그날 전부가
        미퇴근이면 판정 보류(기존 이벤트 유지).

        attendance_id 는 그날 attendance 가 정확히 1건일 때만 기록 (참고용).

        commit 하지 않는다 — 호출자 트랜잭션 소유.
        """
        from app.services.attendance_service import (
            attendance_service,
            compute_net_work_minutes,
        )

        org_id = await self._resolve_org_id(db, store_id)

        attendances = (
            (
                await db.execute(
                    select(Attendance).where(
                        Attendance.store_id == store_id,
                        Attendance.work_date >= start_date,
                        Attendance.work_date <= end_date,
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

        # (user, work_date) 일 단위 그룹핑 — split shift 는 같은 일로 합산
        by_day: dict[tuple[UUID, date], list[Attendance]] = {}
        for att in attendances:
            by_day.setdefault((att.user_id, att.work_date), []).append(att)

        detected: dict[tuple[UUID, date, str], dict] = {}
        hold_keys: set[tuple[UUID, date]] = set()
        for (user_id, work_date), day_atts in by_day.items():
            nets = [
                net
                for att in day_atts
                if (net := compute_net_work_minutes(att, breaks_map.get(att.id, [])))
                is not None
            ]
            if not nets:
                # 전부 미퇴근 — 판정 불가. 기존 이벤트 유지 (void 금지).
                hold_keys.add((user_id, work_date))
                continue
            day_net = sum(nets)
            day_breaks = [
                br for att in day_atts for br in breaks_map.get(att.id, [])
            ]
            # 참고용 attendance 링크 — 1:1 일 때만
            att_id = day_atts[0].id if len(day_atts) == 1 else None

            meal_reason = evaluate_meal_penalty(day_net, day_breaks)
            if meal_reason is not None:
                detected[(user_id, work_date, EVENT_KIND_MEAL_PENALTY)] = {
                    "reason": meal_reason,
                    "attendance_id": att_id,
                }
            rest_reason = evaluate_rest_penalty(day_net, day_breaks)
            if rest_reason is not None:
                detected[(user_id, work_date, EVENT_KIND_REST_PENALTY)] = {
                    "reason": rest_reason,
                    "attendance_id": att_id,
                }

        return await self._upsert(
            db,
            organization_id=org_id,
            store_id=store_id,
            start_date=start_date,
            end_date=end_date,
            kinds=PENALTY_EVENT_KINDS,
            detected=detected,
            hold_keys=hold_keys,
        )

    # ── OT/DT 분류 이벤트 (계산 엔진 출력 반영) ──────────────────

    def _classification_reason(self, day: ClassifiedDay, kind: str) -> str:
        """분류 kind 별 사람이 읽는 reason (영어)."""
        if kind == EVENT_KIND_DAILY_OT:
            parts: list[str] = []
            if day.daily_ot_minutes > 0:
                parts.append(
                    f"{_fmt_hours(day.daily_ot_minutes)}h over "
                    f"{CA_DAILY_OT_HOURS}h/day at 1.5x"
                )
            if day.daily_dt_minutes > 0:
                parts.append(
                    f"{_fmt_hours(day.daily_dt_minutes)}h over "
                    f"{CA_DAILY_DT_HOURS}h/day at 2x"
                )
            return "Daily overtime: " + ", ".join(parts)
        if kind == EVENT_KIND_WEEKLY_OT:
            return (
                f"Weekly overtime: {_fmt_hours(day.weekly_ot_minutes)}h over "
                f"{WEEKLY_OT_HOURS}h/week at 1.5x"
            )
        # seventh_day
        return "7th consecutive day worked in Sun-Sat workweek"

    async def upsert_classification_events(
        self,
        db: AsyncSession,
        store_id: UUID,
        start_date: date,
        end_date: date,
        classified_days: list[ClassifiedDay],
    ) -> EventUpsertSummary:
        """계산 엔진의 일별 분류 출력을 daily_ot/weekly_ot/seventh_day 이벤트로 반영.

        classified_days 는 (store, 범위) 에 대해 authoritative — 범위 내 기존
        분류 이벤트 중 목록이 재현하지 않는 키는 void. penalty kind 와 동일한
        upsert/void/freeze 의미론 (frozen 행 불변, attribution 보존).

        범위 밖 work_date 를 가진 항목은 무시한다 (void 스캔이 범위 한정이라
        범위 밖 upsert 는 비대칭 잔행을 만들 수 있음).

        commit 하지 않는다 — 호출자 트랜잭션 소유.
        """
        org_id = await self._resolve_org_id(db, store_id)

        detected: dict[tuple[UUID, date, str], dict] = {}
        for day in classified_days:
            if not (start_date <= day.work_date <= end_date):
                continue
            present: list[str] = []
            if day.daily_ot_minutes > 0 or day.daily_dt_minutes > 0:
                present.append(EVENT_KIND_DAILY_OT)
            if day.weekly_ot_minutes > 0:
                present.append(EVENT_KIND_WEEKLY_OT)
            if day.seventh_day:
                present.append(EVENT_KIND_SEVENTH_DAY)
            for kind in present:
                detected[(day.user_id, day.work_date, kind)] = {
                    "reason": self._classification_reason(day, kind),
                    "attendance_id": day.attendance_id,
                }

        return await self._upsert(
            db,
            organization_id=org_id,
            store_id=store_id,
            start_date=start_date,
            end_date=end_date,
            kinds=CLASSIFICATION_EVENT_KINDS,
            detected=detected,
            hold_keys=set(),
        )

    # ── 조회 / 태깅 ──────────────────────────────────────────────

    async def list_events(
        self,
        db: AsyncSession,
        store_id: UUID,
        start_date: date,
        end_date: date,
        include_voided: bool = False,
    ) -> list[PayrollEvent]:
        """store + 기간의 이벤트 목록. 기본은 유효(voided IS NULL) 행만."""
        query = (
            select(PayrollEvent)
            .where(
                PayrollEvent.store_id == store_id,
                PayrollEvent.work_date >= start_date,
                PayrollEvent.work_date <= end_date,
            )
            .order_by(
                PayrollEvent.work_date.asc(),
                PayrollEvent.kind.asc(),
                PayrollEvent.created_at.asc(),
            )
        )
        if not include_voided:
            query = query.where(PayrollEvent.voided_at.is_(None))
        return list((await db.execute(query)).scalars().all())

    async def tag_event(
        self,
        db: AsyncSession,
        event_id: UUID,
        attribution: str,
        tagged_by: UUID,
    ) -> PayrollEvent:
        """이벤트에 귀책 태그(E1) 기록 — staff | management.

        frozen(=pay_period_id 부여) 행은 거부 — 확정 후 태깅 변경은 amendment.
        스스로 commit 한다 (단독 API 액션).

        Raises:
            BadRequestError: attribution 값이 잘못됐거나 frozen 행일 때
            NotFoundError: 이벤트가 없을 때
        """
        if attribution not in VALID_ATTRIBUTIONS:
            raise BadRequestError(
                f"Invalid attribution: {attribution}. "
                f"Allowed: {ATTRIBUTION_STAFF}, {ATTRIBUTION_MANAGEMENT}"
            )
        event = await db.get(PayrollEvent, event_id)
        if event is None:
            raise NotFoundError("Payroll event not found")
        if event.pay_period_id is not None:
            raise BadRequestError(
                "This event belongs to a confirmed pay period and cannot be "
                "tagged. Use an amendment to change confirmed payroll data."
            )
        event.attribution = attribution
        event.tagged_by = tagged_by
        event.tagged_at = datetime.now(timezone.utc)
        await db.flush()
        try:
            await db.commit()
            await db.refresh(event)
            return event
        except Exception:
            await db.rollback()
            raise


# 싱글턴 인스턴스 — Singleton instance
payroll_event_service: PayrollEventService = PayrollEventService()
