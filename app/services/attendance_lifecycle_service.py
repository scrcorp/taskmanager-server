"""Attendance Lifecycle Service — schedule 상태 변경에 맞춰 attendance row 동기화.

Eager 모델: schedule이 존재하는 동안 그에 묶인 attendance row 가 하나 존재한다.
schedule이 생성/확정/거부/취소/삭제/전환 될 때 schedule_service 가 이 모듈의
함수들을 호출해서 attendance 를 최신 상태로 유지한다.

규칙:
- row 생성: status 는 현재 시각 + schedule.start_time + late_buffer 기반으로 결정
  (upcoming / late / no_show). 단 schedule.status 가 cancelled/rejected/deleted 면 "cancelled"
- row 복구: schedule 이 cancelled 에서 다시 살아날 때 (revert 등) status 재계산
- row 취소: schedule 이 cancelled/rejected/deleted 되면 status="cancelled"로 마킹
- row 보존: 물리 삭제하지 않음. 이력 유지.
- row 재판정: schedule 시각이 바뀌면 **기록이 있는 row 도** 라벨(status/anomalies)을
  다시 매긴다. 시각(clock_in/clock_out)은 사건이라 절대 바꾸지 않는다 —
  바뀌는 건 그 사건에 붙는 해석뿐이다. 전/후는 attendance timeline 에 남는다.

판정 규칙과 임계값은 이 모듈이 소유하지 않는다. 규칙은
`app/utils/attendance_judgement.py`, 임계값은 `attendance_threshold_service` 하나만
경유한다 — 리터럴을 여기 두거나 직접 초를 깎는 순간 같은 출근이 경로마다 다른
라벨을 받는 사고가 재발한다.
"""

from __future__ import annotations

from datetime import date as date_cls, datetime, time, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import Attendance
from app.models.organization import Store
from app.models.schedule import Schedule
from app.services.attendance_threshold_service import (
    AttendanceThresholds,
    resolve_late_buffer,
    resolve_thresholds,
)
from app.utils.attendance_judgement import (
    ClockInKind,
    ClockOutKind,
    is_late_arrival,
    judge_clock_in,
    judge_clock_out,
)
from app.utils.timezone import floor_to_minute, resolve_schedule_instants

# 조기 출근 강행 라벨 — 코드 문자열의 원본은 attendance_service 하나뿐이다.
from app.services.attendance_service import ANOMALY_EARLY_CLOCK_IN_OVERRIDE

# ── 재판정이 "소유" 하는 라벨 ─────────────────────────────────────────────
# 스케줄 시각에서 파생되는 라벨만 여기 들어간다. 재판정은 이 집합만 매번 새로 계산해
# 교체하고, 나머지(auto_clocked_out / no_break / overlapping_clock_in …)는 **손대지
# 않는다.** anomalies 를 통째 대입하면 auto_clocked_out 이 조용히 사라지고, 그러면
# payroll 확정 게이트 ①(미확인 자동퇴근)이 아무 말 없이 통과한다.
_IN_OWNED_ANOMALIES = ("late", "no_show", ANOMALY_EARLY_CLOCK_IN_OVERRIDE)
_OUT_OWNED_ANOMALIES = ("early_leave", "early_clock_out", "overtime")
RELABEL_OWNED_ANOMALIES = frozenset(_IN_OWNED_ANOMALIES + _OUT_OWNED_ANOMALIES)

# 초과근무 판정 여유 — 예정 근무 길이 + 이 분을 넘으면 overtime.
# attendance_service 의 clock-out 판정과 **같은 규칙**이어야 한다. 갈리면 스케줄을
# 한 번 저장하는 것만으로 overtime 라벨이 붙었다 떨어졌다 한다.
OVERTIME_GRACE_MINUTES = 30

# timeline 에 남는 사유 문구. 콘솔 카드에 그대로 보이므로 영어 고정.
RELABEL_REASON = "Shift time changed"


async def _resolve_late_buffer(db: AsyncSession, organization_id: UUID, store_id: UUID | None) -> int:
    """지각 유예 분 — 해석은 공용 해석기 하나만 쓴다(fallback 기본값도 거기 하나)."""
    return await resolve_late_buffer(
        db, organization_id=organization_id, store_id=store_id
    )


async def _resolve_store_tz(db: AsyncSession, store_id: UUID | None) -> ZoneInfo:
    """store.timezone null이면 organization.timezone fallback. 없으면 UTC."""
    if store_id is None:
        return ZoneInfo("UTC")
    from app.utils.timezone import get_store_day_config
    try:
        tz_name, _ = await get_store_day_config(db, store_id)
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _compute_initial_status(
    schedule_status: str,
    work_date: date_cls,
    start_time: time | None,
    end_time: time | None,
    tz: ZoneInfo,
    late_buffer_min: int,
    now_utc: datetime,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> tuple[str, list[str] | None]:
    """schedule 상태 + 시각 조합으로 attendance.status 와 anomalies 계산."""
    if schedule_status in ("cancelled", "rejected", "deleted"):
        return "cancelled", None
    # 시간이 없으면 일단 upcoming
    if start_at is None and end_at is None and start_time is None and end_time is None:
        return "upcoming", None
    # start_at 우선, 없으면 combine 폴백 (store tz aware)
    sched_start, sched_end = resolve_schedule_instants(
        start_at=start_at, end_at=end_at, work_date=work_date,
        start_time=start_time, end_time=end_time, tz_name=tz.key,
    )

    # late/no_show 판정은 분 단위로만 (초 버림). clock_in/out 은 초까지 저장하되,
    # 초 차이로 정시 출근이 late 로 찍히지 않게 한다.
    now_min = floor_to_minute(now_utc)
    if sched_end and now_min >= sched_end:
        return "no_show", ["no_show"]
    if is_late_arrival(now_min, sched_start, late_buffer=late_buffer_min):
        return "late", ["late"]
    return "upcoming", None


async def ensure_attendance_for_schedule(
    db: AsyncSession,
    schedule: Schedule,
) -> Attendance:
    """Schedule 에 묶인 attendance row 를 생성/복구.

    - row 가 없으면 새로 생성.
    - row 가 이미 있고 status 가 "cancelled" 라면 (schedule 이 revert 된 경우)
      현재 시각 기준으로 status 재계산. clock_in 등 기록 필드는 건드리지 않음.
    - row 가 이미 있고 진행 중(working/on_break/late 등)이면 그대로 둠.
    """
    existing = await db.scalar(
        select(Attendance).where(Attendance.schedule_id == schedule.id)
    )
    now = datetime.now(timezone.utc)
    tz = await _resolve_store_tz(db, schedule.store_id)
    buffer = await _resolve_late_buffer(db, schedule.organization_id, schedule.store_id)
    status, anomalies = _compute_initial_status(
        schedule.status,
        schedule.work_date,
        schedule.start_time,
        schedule.end_time,
        tz,
        buffer,
        now,
        schedule.start_at,
        schedule.end_at,
    )

    if existing is None:
        attendance = Attendance(
            organization_id=schedule.organization_id,
            store_id=schedule.store_id,
            user_id=schedule.user_id,
            schedule_id=schedule.id,
            work_date=schedule.work_date,
            status=status,
            anomalies=anomalies,
        )
        db.add(attendance)
        await db.flush()
        return attendance

    # 이미 존재: cancelled 였으면 부활, 그 외에는 손대지 않음 (clock-in 기록 보존)
    if existing.status == "cancelled":
        existing.status = status
        existing.anomalies = anomalies
    return existing


async def status_after_time_clear(
    db: AsyncSession,
    attendance: Attendance,
) -> tuple[str, list[str] | None]:
    """시간 기록을 모두 지운 attendance 가 가져야 할 status/anomalies 를 계산.

    `clear_times` 전용. 기록이 사라진 row 는 "출근 전" 상태로 돌아가야 하는데,
    그게 upcoming 인지 late 인지 no_show 인지는 스케줄 시각과 현재 시각이 정한다 —
    새 row 를 만들 때와 같은 판정이므로 `_compute_initial_status` 를 그대로 쓴다.

    스케줄이 없는 row(워크인)는 호출 측에서 미리 거른다. 방어적으로 upcoming 반환.
    """
    if attendance.schedule_id is None:
        return "upcoming", None
    schedule = await db.scalar(
        select(Schedule).where(Schedule.id == attendance.schedule_id)
    )
    if schedule is None:
        return "upcoming", None
    tz = await _resolve_store_tz(db, schedule.store_id)
    buffer = await _resolve_late_buffer(db, schedule.organization_id, schedule.store_id)
    return _compute_initial_status(
        schedule.status,
        schedule.work_date,
        schedule.start_time,
        schedule.end_time,
        tz,
        buffer,
        datetime.now(timezone.utc),
        schedule.start_at,
        schedule.end_at,
    )


def _judge_overtime(
    total_work_minutes: int | None,
    scheduled_start: datetime | None,
    scheduled_end: datetime | None,
) -> bool | None:
    """초과근무인가. 판정 불가면 None (기존 라벨을 그대로 두라는 뜻).

    "예정 길이 + 30분" 기준이라 스케줄 길이가 바뀌면 그대로 뒤집힌다 — 그래서
    clock_out 계열 라벨을 재판정에서 빼면 안 된다. 한 row 안에서 앞쪽 라벨만 새
    스케줄, 뒤쪽 라벨은 옛 스케줄이 되어 원인 B 를 절반만 고친 상태가 된다.
    """
    if total_work_minutes is None or scheduled_start is None or scheduled_end is None:
        return None
    scheduled_minutes = (scheduled_end - scheduled_start).total_seconds() / 60
    return total_work_minutes > scheduled_minutes + OVERTIME_GRACE_MINUTES


def relabel_after_schedule_change(
    *,
    status: str,
    clock_in: datetime | None,
    clock_out: datetime | None,
    anomalies: list[str] | None,
    total_work_minutes: int | None,
    scheduled_start: datetime | None,
    scheduled_end: datetime | None,
    thresholds: AttendanceThresholds,
) -> tuple[str, list[str] | None]:
    """**기록이 있는** attendance 의 라벨을 새 스케줄 기준으로 다시 매긴다 (순수 함수).

    `_compute_initial_status` 를 재사용할 수 없는 이유 — 그건 "아직 출근 안 한" row
    전용이라 early 개념이 아예 없다(`is_late_arrival` 만 쓴다). 그대로 돌리면 조기
    출근이 무조건 on_time 으로 재판정되어 `early_clock_in_override` 가 영구 소멸한다.

    **시각(clock_in/clock_out)은 절대 바꾸지 않는다.** 실제로 일어난 사건이므로
    스케줄을 고쳤다고 달라질 수 있는 값이 아니다. 바뀌는 건 그 사건에 붙는 해석뿐이다.

    판정 불가(스케줄 시각 미상, 미퇴근)일 때는 **기존 라벨을 보존**한다. 모르면
    지우는 게 아니라 두는 쪽이 맞다 — 지우면 근거 없이 사라진 라벨을 복구할 길이 없다.

    Args:
        status: 현재 attendance.status.
        clock_in: 실제 출근 시각 (호출자가 not None 을 보장한다).
        clock_out: 실제 퇴근 시각. None 이면 clock-out 계열 라벨은 판정하지 않는다.
        anomalies: 현재 라벨 목록.
        total_work_minutes: 저장된 총 근무 분 (overtime 판정용).
        scheduled_start / scheduled_end: **새** 스케줄의 절대시각.
        thresholds: 매장 임계값 3종 (호출자가 1회 조회해 주입).

    Returns:
        (새 status, 새 anomalies) — anomalies 가 비면 None.
    """
    old = list(anomalies or [])
    # 소유하지 않는 라벨은 순서까지 그대로 살린다.
    kept = [a for a in old if a not in RELABEL_OWNED_ANOMALIES]
    owned: list[str] = []

    # ── clock-in 계열 ─────────────────────────────────────────────────────
    in_verdict = judge_clock_in(
        clock_in,
        scheduled_start,
        late_buffer=thresholds.late_buffer,
        early_threshold=thresholds.early_clock_in,
    )
    if in_verdict.kind is ClockInKind.UNKNOWN:
        owned += [a for a in old if a in _IN_OWNED_ANOMALIES]
    elif in_verdict.kind is ClockInKind.EARLY:
        owned.append(ANOMALY_EARLY_CLOCK_IN_OVERRIDE)
    elif in_verdict.kind is ClockInKind.LATE:
        owned.append("late")
    # ON_TIME 은 라벨 없음. no_show 는 출근 기록이 있는 이상 성립하지 않으므로
    # 여기서 절대 다시 붙지 않는다 (소유 라벨이라 위에서 이미 걷혔다).

    # ── clock-out 계열 ────────────────────────────────────────────────────
    if clock_out is None:
        # 아직 끝나지 않은 근무 — 조퇴/초과근무는 판정 대상이 아니다. 기존 값 보존.
        owned += [a for a in old if a in _OUT_OWNED_ANOMALIES]
    else:
        out_verdict = judge_clock_out(
            clock_out, scheduled_end, early_leave_threshold=thresholds.early_leave
        )
        if out_verdict.kind is ClockOutKind.UNKNOWN:
            owned += [a for a in old if a in _OUT_OWNED_ANOMALIES]
        else:
            if out_verdict.kind is ClockOutKind.EARLY:
                # `early_leave`(앱/서비스 경로)와 `early_clock_out`(키오스크)이 공존한다.
                # 재판정에서 코드를 통일하지 않는다 — 여기서 바꾸면 콘솔 배지·필터가
                # 조용히 갈린다. 정규화는 별건이다.
                owned.append(
                    "early_clock_out" if "early_clock_out" in old else "early_leave"
                )
            overtime = _judge_overtime(
                total_work_minutes, scheduled_start, scheduled_end
            )
            if overtime is True or (overtime is None and "overtime" in old):
                owned.append("overtime")

    # ── status ────────────────────────────────────────────────────────────
    # 퇴근했거나 휴식 중인 row 의 status 는 판정이 정하는 값이 아니다 (사건이 정한다).
    # 근무 중인 row 만 late ↔ working 을 오간다.
    new_status = status
    if (
        clock_out is None
        and status in ("working", "late")
        and in_verdict.kind is not ClockInKind.UNKNOWN
    ):
        new_status = "late" if in_verdict.kind is ClockInKind.LATE else "working"

    merged = kept + [a for a in owned if a not in kept]
    return new_status, (merged or None)


def _anomalies_text(anomalies: list[str] | None) -> str | None:
    """라벨 목록 → 이력에 남길 문자열. 순서 차이가 가짜 변경으로 보이지 않게 정렬한다."""
    if not anomalies:
        return None
    return ", ".join(sorted(anomalies))


async def _relabel_recorded_attendance(
    db: AsyncSession,
    attendance: Attendance,
    schedule: Schedule,
    *,
    tz: ZoneInfo,
    actor_id: UUID | None,
) -> None:
    """clock_in 이 있는 row 의 라벨 재판정 + 이력 기록."""
    from app.services import attendance_timeline as tl

    thresholds = await resolve_thresholds(
        db,
        organization_id=schedule.organization_id,
        store_id=schedule.store_id,
    )
    sched_start, sched_end = resolve_schedule_instants(
        start_at=schedule.start_at,
        end_at=schedule.end_at,
        work_date=schedule.work_date,
        start_time=schedule.start_time,
        end_time=schedule.end_time,
        tz_name=tz.key,
    )

    before_status = attendance.status
    before_anomalies = list(attendance.anomalies or [])

    new_status, new_anomalies = relabel_after_schedule_change(
        status=before_status,
        clock_in=attendance.clock_in,
        clock_out=attendance.clock_out,
        anomalies=before_anomalies,
        total_work_minutes=attendance.total_work_minutes,
        scheduled_start=sched_start,
        scheduled_end=sched_end,
        thresholds=thresholds,
    )

    attendance.status = new_status
    attendance.anomalies = new_anomalies

    # ── 조기 출근 확인 도장 처리 (D11 의 "조용히 사라지면 안 된다") ──────────
    #
    # 결정: `early_clock_in_override` 의 **유무가 변하면** 확인 도장을 항상 리셋한다.
    #
    #   - 라벨이 걷혔을 때: 확인의 대상이 사라졌다. `clear_times` 가 지워진 시각의
    #     확인 도장을 함께 지우는 것과 같은 이유다. 남겨두면 나중에 스케줄이 되돌려져
    #     라벨이 다시 붙었을 때, **아무도 확인한 적 없는 건이 "확인됨" 으로**
    #     payroll 게이트 ⑥ 를 통과한다 (게이트는 anomaly + confirmed_at IS NULL 짝으로만
    #     판정한다 — payroll_confirm_service._early_clock_in_dates).
    #   - 라벨이 새로 붙었을 때: 도장이 없어야 게이트가 걸린다. 원래 NULL 이므로
    #     리셋은 무해하고, 규칙을 "변하면 리셋" 하나로 두는 편이 훨씬 안전하다.
    #
    # **사유는 사라지지 않는다.** 조기 출근 사유 문자열은 attendance_corrections
    # (append-only 이력)에 남아 있고 이 함수는 거기에 쓰기만 한다. 라벨이 걷힌 뒤
    # "왜 사유 카드는 있는데 early 라벨이 없나" 는 아래 재판정 이력 행이 설명한다.
    had_early = ANOMALY_EARLY_CLOCK_IN_OVERRIDE in before_anomalies
    has_early = ANOMALY_EARLY_CLOCK_IN_OVERRIDE in (new_anomalies or [])
    if had_early != has_early:
        attendance.early_clock_in_confirmed_at = None
        attendance.early_clock_in_confirmed_by = None
        # 요청자(D9) 도 같은 이유로 되돌린다. 라벨이 걷혔다면 "누가 일찍 오라고 했나" 는
        # 더 이상 이 기록에 대한 답이 아니고, 라벨이 새로 붙었다면 아무도 부른 적이 없다.
        # 사유 문자열은 attendance_corrections 에 그대로 남아 경위를 설명한다.
        attendance.early_clock_in_requested_by = None

    # 한 번의 스케줄 수정 = 카드 한 장이므로 group_id 는 하나.
    # 새 action 상수를 만들지 않는다 — 콘솔은 모르는 action 을 raw 문자열로 그린다.
    group = tl.new_group()
    tl.record_status(
        db,
        attendance_id=attendance.id,
        group_id=group,
        action=tl.ACTION_MODIFY,
        before=before_status,
        after=new_status,
        reason=RELABEL_REASON,
        by_user_id=actor_id,
    )
    tl.record(
        db,
        attendance_id=attendance.id,
        group_id=group,
        action=tl.ACTION_MODIFY,
        field_name=tl.FIELD_ANOMALIES,
        before=tl.plain_value(_anomalies_text(before_anomalies)),
        after=tl.plain_value(_anomalies_text(new_anomalies)),
        reason=RELABEL_REASON,
        by_user_id=actor_id,
    )
    # before == after 인 행은 `record` 가 걸러내므로, "시간은 바꿨지만 라벨은 그대로"
    # 인 흔한 경우에 이력 소음이 남지 않는다.
    await db.flush()


async def recompute_attendance_for_schedule_change(
    db: AsyncSession,
    schedule: Schedule,
    *,
    actor_id: UUID | None = None,
) -> None:
    """Schedule.operating_day / start_at / end_at 이 바뀐 후 attendance 라벨 재계산.

    두 갈래다:

    1. **미출근 row** — 새 시간 기준으로 upcoming/late/no_show 를 다시 정한다.
       cron 의 승격/강등을 기다리지 않고 즉시 일관된 상태를 보장한다.
    2. **기록이 있는 row** (clock_in 존재) — status/anomalies 를 다시 매기고 전/후를
       타임라인에 남긴다 (D11). 예전에는 "출근 기록 보존" 을 이유로 통째 건너뛰었는데,
       보존해야 하는 건 *시각* 이지 *라벨* 이 아니었다. 그 결과 17시 스케줄을 14시로
       고쳐도 15시 출근이 계속 "조기 출근" 으로 남았다 (원인 B).

    시각(clock_in/clock_out/break)은 어느 갈래에서도 건드리지 않는다.

    `work_date` 는 미출근 row 에서만 스케줄을 따라간다. 기록이 있는 row 의 work_date 는
    payroll 기간 귀속 키이고(모든 게이트 쿼리가 `work_date BETWEEN`), 그 시각에 실제로
    일이 일어난 영업일이다 — 라벨 재판정이 급여 귀속일을 조용히 옮기면 안 된다.

    확정된 pay period 는 별도 가드가 필요 없다. `update_entry` 가 진입 즉시
    `ensure_schedule_not_locked` 로 409 를 던지므로 여기까지 오지 않는다 (D7 과 정합).
    """
    existing = await db.scalar(
        select(Attendance).where(Attendance.schedule_id == schedule.id)
    )
    if existing is None or existing.status == "cancelled":
        return

    tz = await _resolve_store_tz(db, schedule.store_id)

    if existing.clock_in is not None:
        await _relabel_recorded_attendance(
            db, existing, schedule, tz=tz, actor_id=actor_id
        )
        return

    if existing.status not in ("upcoming", "late", "no_show"):
        return
    now = datetime.now(timezone.utc)
    buffer = await _resolve_late_buffer(db, schedule.organization_id, schedule.store_id)
    status, anomalies = _compute_initial_status(
        schedule.status,
        schedule.work_date,
        schedule.start_time,
        schedule.end_time,
        tz,
        buffer,
        now,
        schedule.start_at,
        schedule.end_at,
    )
    existing.status = status
    existing.anomalies = anomalies
    existing.work_date = schedule.work_date
    await db.flush()


async def cancel_attendance_for_schedule(
    db: AsyncSession,
    schedule_id: UUID,
    by_user_id: UUID | None = None,
    reason: str | None = None,
) -> None:
    """Schedule 이 cancel/reject/delete 되었을 때 attendance.status 를 cancelled 로 마킹.

    row 가 없으면 아무것도 하지 않는다 (이미 정리되었거나 애초에 생성 안됨).

    D10-2 — 취소와 삭제는 다른 개념이지만 **둘 다 기록에 남아야 한다.** 근태는 급여의
    근거 자료라 "있었는데 사라졌다"가 데이터로 남지 않으면 나중에 복원도 반박도 못 한다.
    예전 키오스크 삭제 경로는 attendance 를 hard delete 해서 breaks/corrections 까지
    CASCADE 로 함께 소멸했다 — 그 비대칭을 없애고 여기 한 곳으로 모았다.

    이미 cancelled 인 row 는 `tl.record` 가 before==after 로 걸러 소음을 남기지 않는다.
    """
    existing = await db.scalar(
        select(Attendance).where(Attendance.schedule_id == schedule_id)
    )
    if existing is None:
        return
    from app.services import attendance_timeline as tl

    before = existing.status
    existing.status = "cancelled"
    # 이전 anomalies 는 유지 (late 후 cancelled 같은 이력)
    tl.record(
        db,
        attendance_id=existing.id,
        group_id=tl.new_group(),
        action=tl.ACTION_CANCEL,
        field_name=tl.FIELD_STATUS,
        before=tl.plain_value(before),
        after=tl.plain_value(existing.status),
        reason=reason,
        by_user_id=by_user_id,
    )
    await db.flush()


async def reassign_attendance_user(
    db: AsyncSession,
    schedule_id: UUID,
    new_user_id: UUID,
) -> None:
    """Switch Schedule 등으로 schedule.user_id 가 바뀌었을 때 attendance.user_id 동기화.

    진행 중 기록이 있어도 user_id 만 옮김 (clock-in 등은 원래 사람 것이라 일반적이면 switch
    전에 정리돼야 하지만 정책은 상위에서 결정; 여기서는 단순 동기화).
    """
    existing = await db.scalar(
        select(Attendance).where(Attendance.schedule_id == schedule_id)
    )
    if existing is None:
        return
    existing.user_id = new_user_id
    await db.flush()
