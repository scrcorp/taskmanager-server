"""Attendance Cron Service — 자동 상태 전환.

Eager 모델 전환 후 역할이 단순해졌다:
- attendance row 는 schedule 생성 시점에 이미 존재.
- API 응답 시점에 effective status 가 계산(upcoming → soon/late)돼서 UX 에 바로 반영.
- 이 cron 은 DB persist 용: upcoming 인데 이미 시간이 지나 late/no_show 로 굳어진
  row 를 실제로 해당 status 로 업데이트 한다. 정산/통계 쿼리가 실시간 계산 없이
  DB 만 봐도 되도록.

APScheduler 진입점은 `run_attendance_state_tick()`.
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.attendance import Attendance
from app.models.organization import Store
from app.models.schedule import Schedule
from app.services.attendance_threshold_service import resolve_late_buffer
from app.utils.attendance_judgement import is_late_arrival
from app.utils.settings_resolver import SettingNotRegisteredError, resolve_setting
from app.utils.timezone import (
    floor_to_minute,
    get_store_day_config,
    minutes_between,
    resolve_schedule_instants,
)


logger = logging.getLogger("uvicorn.error")

DEFAULT_AUTO_CLOCK_OUT_AFTER_MINUTES = 30


async def _persist_late_and_no_show(db: AsyncSession) -> tuple[int, int]:
    """미출근 attendance 의 status 를 시간 경과에 따라 late / no_show 로 승격.

    승격 대상 status: 'upcoming' 또는 이미 'late' (clock-in 안 한 상태).
    'working' / 'on_break' 등 clock-in 후 상태는 건드리지 않음.
    """
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()
    two_days_ago = today_utc - timedelta(days=2)

    rows = await db.execute(
        select(Attendance, Schedule, Store)
        .join(Schedule, Schedule.id == Attendance.schedule_id)
        .outerjoin(Store, Store.id == Attendance.store_id)
        .where(
            Attendance.status.in_(["upcoming", "late"]),
            Attendance.work_date >= two_days_ago,
            Attendance.work_date <= today_utc,
            Schedule.start_at.isnot(None),
        )
    )
    rows_list = list(rows.all())

    late_count = 0
    no_show_count = 0

    # (L3) 확정된 pay period 안의 row 는 cron 도 건드리지 않는다 —
    # (store, work_date) 별 1회만 조회하는 저비용 캐시.
    from app.services.payroll_lock_service import is_locked_cached
    lock_cache: dict = {}

    for att, sch, store in rows_list:
        if await is_locked_cached(
            db, lock_cache, store_id=att.store_id, work_date=att.work_date
        ):
            continue
        # store.timezone 이 None 이면 organization.timezone 으로 fallback —
        # 그냥 store.timezone or "UTC" 하면 매장 미설정 시 UTC 로 떨어져
        # sched_end 시각을 잘못 계산한다.
        tz_name, _ = await get_store_day_config(db, att.store_id)
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        sched_start, sched_end = resolve_schedule_instants(
            start_at=sch.start_at, end_at=sch.end_at, work_date=sch.work_date,
            start_time=sch.start_time, end_time=sch.end_time, tz_name=tz.key,
        )

        # late_buffer setting per org/store (해석기 하나 — fallback 기본값도 거기 하나)
        late_buffer = await resolve_late_buffer(
            db, organization_id=att.organization_id, store_id=att.store_id
        )

        # no_show/late 판정은 분 단위로만 (초 버림) — 정시가 초 차이로 late 가 되지 않게.
        now_min = floor_to_minute(now_utc)
        # 1) sched_end 가 지났으면 no_show 로 강등.
        #    단 clock_in 이 이미 있으면(출근 완료) "late" 그대로 유지 — 출근한 직원을
        #    no_show 로 표시하면 키오스크 "Clocked In" 섹션에서 사라진다.
        if sched_end is not None and now_min >= sched_end:
            # clock_in 또는 clock_out 이 있으면(실제 근무 기록 존재) no_show 로
            # 강등하지 않는다 — 실제 기록이 status 정합화(reconcile)보다 우선.
            if att.clock_in is not None or att.clock_out is not None:
                continue
            if att.status != "no_show":
                att.status = "no_show"
                anoms = list(att.anomalies or [])
                if "no_show" not in anoms:
                    anoms.append("no_show")
                att.anomalies = anoms or None
                no_show_count += 1
            continue

        # 2) 아직 sched_end 전이면 late_buffer 지났을 때 upcoming → late
        if att.status == "upcoming" and is_late_arrival(
            now_min, sched_start, late_buffer=late_buffer
        ):
            att.status = "late"
            anoms = list(att.anomalies or [])
            if "late" not in anoms:
                anoms.append("late")
            att.anomalies = anoms or None
            late_count += 1

    if late_count or no_show_count:
        await db.commit()
    return late_count, no_show_count


async def _auto_clock_out_overdue(db: AsyncSession) -> int:
    """Clock-out을 잊은 attendance를 sched_end + auto_after_minutes 지나면 자동 종료.

    - 대상: status in (working, on_break) AND clock_in IS NOT NULL AND clock_out IS NULL
    - 조건: schedule.end_time이 있고, now >= sched_end + auto_after_minutes
    - 처리:
        clock_out = sched_end (사용자 요청: "원래 퇴근시간으로")
        status = "clocked_out"
        진행중인 break는 sched_end 시점으로 종료
        anomalies에 "auto_clocked_out" 추가
        total_work_minutes / total_break_minutes 재계산
    """
    from app.models.attendance_break import AttendanceBreak

    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()
    two_days_ago = today_utc - timedelta(days=2)

    rows = await db.execute(
        select(Attendance, Schedule, Store)
        .join(Schedule, Schedule.id == Attendance.schedule_id)
        .outerjoin(Store, Store.id == Attendance.store_id)
        .where(
            # late + clock_in 있는 케이스도 포함 — 늦게 출근한 후 clock-out 안 한 경우.
            Attendance.status.in_(["working", "on_break", "late"]),
            Attendance.clock_in.isnot(None),
            Attendance.clock_out.is_(None),
            Attendance.work_date >= two_days_ago,
            Attendance.work_date <= today_utc,
            Schedule.end_at.isnot(None),
        )
    )
    rows_list = list(rows.all())
    auto_count = 0

    # (D15) 겹쳐 열린 근무는 자동 마감하지 않는다.
    # 자동으로 닫으면 두 shift 모두 "그럴듯하게 완결된 기록" 이 되어 **이중 지급이
    # 자동화**된다. skip 하면 open_shift 게이트가 대신 걸리고 미퇴근 알림이 매니저를
    # 계속 찌른다 — 의도한 압박이다.
    # 대상 row 의 짝이 여기 쿼리(Schedule.end_at 필수) 밖에 있을 수 있으므로 그 사람의
    # **열린 근무 전부**를 한 번에 모아 겹침을 계산한다(루프 안 추가 쿼리 없음).
    overlapping_att_ids: set = set()
    candidate_user_ids = {att.user_id for att, _s, _st in rows_list if att.user_id}
    if candidate_user_ids:
        from app.utils.attendance_overlap import overlapping_keys_from_rows

        open_rows = (
            await db.execute(
                select(
                    Attendance.id,
                    Attendance.user_id,
                    Attendance.clock_in,
                    Attendance.clock_out,
                ).where(
                    Attendance.user_id.in_(candidate_user_ids),
                    Attendance.work_date >= two_days_ago - timedelta(days=1),
                    Attendance.work_date <= today_utc,
                    Attendance.status != "cancelled",
                    Attendance.clock_in.is_not(None),
                )
            )
        ).all()
        by_user: dict = {}
        for att_id, user_id, clock_in, clock_out in open_rows:
            by_user.setdefault(user_id, []).append((att_id, clock_in, clock_out))
        for user_rows in by_user.values():
            overlapping_att_ids |= overlapping_keys_from_rows(user_rows, now=now_utc)

    # F9: 틱마다 행 수만큼 resolve 하지 않도록 매장당 1회 resolve 후 캐시.
    auto_enabled_cache: dict = {}

    # (L3) 확정된 pay period 안의 row 는 자동퇴근 처리하지 않는다 (저비용 skip).
    from app.services.payroll_lock_service import is_locked_cached
    lock_cache: dict = {}

    for att, sch, store in rows_list:
        if await is_locked_cached(
            db, lock_cache, store_id=att.store_id, work_date=att.work_date
        ):
            continue
        if att.id in overlapping_att_ids:
            # 겹친 근무 — 사람이 한쪽을 정정/취소해야 한다. 자동 마감 금지(위 주석).
            logger.info(
                f"[attendance_cron] skip auto clock-out for {att.id}: "
                "overlaps another open shift"
            )
            continue
        # N2: 매장의 auto_clock_out_enabled 토글 가드. OFF 매장은 자동 퇴근 skip
        # (미퇴근 관리자 알림은 별도 경로에서 유지 — D11).
        if att.store_id in auto_enabled_cache:
            auto_enabled = auto_enabled_cache[att.store_id]
        else:
            try:
                raw_enabled = await resolve_setting(
                    db,
                    key="attendance.auto_clock_out_enabled",
                    organization_id=att.organization_id,
                    store_id=att.store_id,
                )
                auto_enabled = bool(raw_enabled) if raw_enabled is not None else True
            except (SettingNotRegisteredError, TypeError, ValueError):
                auto_enabled = True
            auto_enabled_cache[att.store_id] = auto_enabled
        if not auto_enabled:
            continue

        # 동일하게 helper 사용 — store.timezone null 이면 org.timezone fallback.
        tz_name, _ = await get_store_day_config(db, att.store_id)
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        sched_start, sched_end = resolve_schedule_instants(
            start_at=sch.start_at, end_at=sch.end_at, work_date=sch.work_date,
            start_time=sch.start_time, end_time=sch.end_time, tz_name=tz.key,
        )

        try:
            raw = await resolve_setting(
                db,
                key="attendance.auto_clock_out_after_minutes",
                organization_id=att.organization_id,
                store_id=att.store_id,
            )
            after_minutes = int(raw) if raw is not None else DEFAULT_AUTO_CLOCK_OUT_AFTER_MINUTES
        except (SettingNotRegisteredError, TypeError, ValueError):
            after_minutes = DEFAULT_AUTO_CLOCK_OUT_AFTER_MINUTES

        if now_utc < sched_end + timedelta(minutes=after_minutes):
            continue

        # AL-4 가드 1 (세션 내): 어떤 경로로든 clock_out 이 이미 있으면 절대
        # 덮어쓰지 않는다. 쿼리에서 clock_out IS NULL 로 걸렀지만 방어적으로 재확인.
        if att.clock_out is not None:
            continue

        # 자동 clock-out 시점은 sched_end (UTC로 저장)
        cutoff = sched_end.astimezone(timezone.utc)

        # AL-5 가드: cutoff 가 clock_in 보다 이르면 자동 마감하지 않는다.
        #
        # 종료시각이 지난 shift 에 지각 출근하면(shift 선택으로 정상 경로가 됐다)
        # 곧바로 이 크론의 대상이 되는데, 그때 clock_out=sched_end 로 찍으면
        # **clock_out < clock_in** 인 음수 근무가 만들어진다. break 는 이미
        # `max(cutoff, started_at)` 로 같은 사고를 막고 있었지만 clock_out 에는
        # 그 보호가 없었다.
        #
        # 0분으로 마감하지도 않는다 — 실제로는 일하고 있는 사람이라 근무를 지워버리는
        # 셈이 된다. 열어 둔 채 미퇴근 알림에 맡기고 사람이 정리하게 한다.
        if att.clock_in is not None and cutoff <= att.clock_in:
            logger.info(
                f"[attendance_cron] skip auto clock-out for {att.id}: "
                f"cutoff {cutoff.isoformat()} <= clock_in {att.clock_in.isoformat()} "
                "(late clock-in after scheduled end)"
            )
            continue

        # AL-4 가드 2 (write-time, 원자적): fetch 이후 per-row await 동안 직원이
        # 실제로 clock-out 했을 수 있다 (race window). WHERE clock_out IS NULL
        # 조건부 UPDATE 로 원자적으로 잠금 — 동시 clock-out 이 먼저 커밋됐으면
        # rowcount=0 이 되고, 이 attendance 는 일절 수정하지 않는다
        # (break/이상치/correction 도 건드리지 않음 → 급여는 실제 퇴근시각 기준 유지).
        was_on_break = att.status == "on_break"
        claim = await db.execute(
            update(Attendance)
            .where(
                Attendance.id == att.id,
                Attendance.clock_out.is_(None),
            )
            .values(
                clock_out=cutoff,
                clock_out_timezone=tz_name,
                status="clocked_out",
            )
        )
        if claim.rowcount == 0:
            # 실제 clock-out 이 이미 기록됨 — 세션 내 stale 상태 폐기 후 skip.
            # (expire 후 attribute 접근은 async 세션에서 lazy load 를 유발하므로
            #  id 를 먼저 캡처해 로그에 사용)
            att_id = att.id
            db.expire(att)
            logger.info(
                f"[attendance_cron] skip auto clock-out for {att_id}: "
                "clock_out already set (raced with real clock-out)"
            )
            continue

        # 진행중 break 종료 (cutoff 기준) — claim 성공한 row 에만 적용.
        if was_on_break:
            br_rows = await db.execute(
                select(AttendanceBreak).where(
                    AttendanceBreak.attendance_id == att.id,
                    AttendanceBreak.ended_at.is_(None),
                )
            )
            for br in br_rows.scalars().all():
                end_at = max(cutoff, br.started_at)
                br.ended_at = end_at
                br.duration_minutes = minutes_between(br.started_at, end_at)
            att.break_end = cutoff

        # ORM 객체도 동기화 (synchronize_session 전략과 무관하게 일관 보장)
        att.clock_out = cutoff
        att.clock_out_timezone = tz_name
        att.status = "clocked_out"
        if att.clock_in is not None:
            att.total_work_minutes = minutes_between(att.clock_in, cutoff)

        # break 누적 재계산
        br_sum = await db.execute(
            select(AttendanceBreak).where(AttendanceBreak.attendance_id == att.id)
        )
        att.total_break_minutes = sum(
            (br.duration_minutes or 0) for br in br_sum.scalars().all()
        )

        anoms = list(att.anomalies or [])
        if "auto_clocked_out" not in anoms:
            anoms.append("auto_clocked_out")
        att.anomalies = anoms or None

        # timeline 에 기록 — system actor (corrected_by=NULL).
        # 상태 전이 + clock_out 값 전이를 한 그룹으로. 이전 상태는 on_break 였을 수도
        # 있으므로 "working" 하드코딩하지 않고 실제 값을 쓴다.
        from app.services import attendance_timeline as tl
        group = tl.new_group()
        auto_reason = f"Shift ended {after_minutes} min ago"
        tl.record_status(
            db,
            attendance_id=att.id,
            group_id=group,
            action=tl.ACTION_AUTO_CLOCK_OUT,
            before="on_break" if was_on_break else "working",
            after=att.status,
            reason=auto_reason,
            by_user_id=None,
        )
        tl.record(
            db,
            attendance_id=att.id,
            group_id=group,
            action=tl.ACTION_AUTO_CLOCK_OUT,
            field_name=tl.FIELD_CLOCK_OUT,
            before=tl.NONE,
            after=tl.dt_value(cutoff),
            reason=auto_reason,
            by_user_id=None,
        )

        auto_count += 1

    if auto_count:
        await db.commit()
    return auto_count


async def _reconcile_closed_attendance_status(db: AsyncSession) -> int:
    """clock_out 이 이미 있는데 status 가 아직 open(working/on_break/late)인
    stale row 의 status 만 clocked_out 으로 정합화한다 (AL-4).

    - clock_out / 시각·분 필드는 절대 수정하지 않는다 — 실제 퇴근 기록이 진실.
    - break 도 건드리지 않는다 (이미 닫힌 attendance 의 break 는 cron 소관 아님).
    - 이렇게 정합화해 두면 status 기반 선택을 쓰는 다른 경로가 닫힌 row 를
      다시 집어 자동 퇴근으로 덮어쓸 여지가 없어진다.
    """
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()
    two_days_ago = today_utc - timedelta(days=2)

    result = await db.execute(
        update(Attendance)
        .where(
            Attendance.status.in_(["working", "on_break", "late"]),
            Attendance.clock_out.isnot(None),
            Attendance.work_date >= two_days_ago,
            Attendance.work_date <= today_utc,
        )
        .values(status="clocked_out")
    )
    count = result.rowcount or 0
    if count:
        await db.commit()
    return count


async def _alert_overdue_clock_outs(db: AsyncSession) -> int:
    """sched_end 가 지났는데 clock-out 안 한 attendance 에 대해 매니저에게
    in-app 알림. 같은 attendance 에 대해 alert_interval_minutes 가 지난 후에만
    다시 발송한다 (중복 방지).
    """
    from app.models.alert import Alert
    from app.models.user import User, Role
    from app.repositories.alert_repository import alert_repository
    from app.core.permissions import GM_PRIORITY

    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()
    two_days_ago = today_utc - timedelta(days=2)

    rows = await db.execute(
        select(Attendance, Schedule)
        .join(Schedule, Schedule.id == Attendance.schedule_id)
        .where(
            Attendance.status.in_(["working", "on_break", "late"]),
            Attendance.clock_in.isnot(None),
            Attendance.clock_out.is_(None),
            Attendance.work_date >= two_days_ago,
            Attendance.work_date <= today_utc,
            Schedule.end_at.isnot(None),
        )
    )
    rows_list = list(rows.all())
    alert_count = 0

    for att, sch in rows_list:
        tz_name, _ = await get_store_day_config(db, att.store_id)
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        sched_start, sched_end = resolve_schedule_instants(
            start_at=sch.start_at, end_at=sch.end_at, work_date=sch.work_date,
            start_time=sch.start_time, end_time=sch.end_time, tz_name=tz.key,
        )
        if sched_end is None or now_utc < sched_end:
            continue

        try:
            raw = await resolve_setting(
                db,
                key="attendance.alert_interval_minutes",
                organization_id=att.organization_id,
                store_id=att.store_id,
            )
            interval_min = int(raw) if raw is not None else 10
        except (SettingNotRegisteredError, TypeError, ValueError):
            interval_min = 10
        if interval_min <= 0:
            continue  # 0 이하면 알림 비활성

        # 같은 attendance 에 대한 가장 최근 알림 시점.
        last_alert_at = await db.scalar(
            select(Alert.created_at)
            .where(
                Alert.organization_id == att.organization_id,
                Alert.type == "attendance_overdue",
                Alert.reference_type == "attendance",
                Alert.reference_id == att.id,
            )
            .order_by(Alert.created_at.desc())
            .limit(1)
        )
        if last_alert_at is not None and (now_utc - last_alert_at) < timedelta(minutes=interval_min):
            continue

        # 받는 사람: organization 의 모든 GM+ active 사용자.
        mgr_rows = await db.execute(
            select(User.id)
            .join(Role, Role.id == User.role_id)
            .where(
                User.organization_id == att.organization_id,
                User.is_active == True,  # noqa: E712
                Role.priority <= GM_PRIORITY,
            )
        )
        manager_ids = [r[0] for r in mgr_rows.all()]
        if not manager_ids:
            continue

        user_name_row = await db.scalar(
            select(User.full_name).where(User.id == att.user_id)
        )
        user_name = (user_name_row or "A staff member")
        sched_end_local = sched_end.astimezone(tz).strftime("%H:%M")
        overdue_minutes = minutes_between(sched_end, now_utc)
        message = (
            f"{user_name} hasn't clocked out (scheduled end {sched_end_local}, "
            f"{overdue_minutes} min overdue)"
        )

        for uid in manager_ids:
            await alert_repository.create_alert(
                db,
                organization_id=att.organization_id,
                user_id=uid,
                alert_type="attendance_overdue",
                message=message,
                reference_type="attendance",
                reference_id=att.id,
            )
            alert_count += 1

    if alert_count > 0:
        await db.commit()
    return alert_count


async def run_attendance_state_tick() -> None:
    """APScheduler에서 호출되는 진입점. 1분마다 실행."""
    try:
        async with async_session() as db:
            # AL-4: status/clock_out 불일치 row 를 먼저 정합화 —
            # 이후 status 기반 선택이 닫힌 row 를 집을 수 없게 한다.
            reconciled_cnt = await _reconcile_closed_attendance_status(db)
            late_cnt, no_show_cnt = await _persist_late_and_no_show(db)
            auto_out_cnt = await _auto_clock_out_overdue(db)
            alert_cnt = await _alert_overdue_clock_outs(db)
            if reconciled_cnt or late_cnt or no_show_cnt or auto_out_cnt or alert_cnt:
                logger.info(
                    f"[attendance_cron] reconciled={reconciled_cnt} "
                    f"persist late={late_cnt} no_show={no_show_cnt} "
                    f"auto_clock_out={auto_out_cnt} overdue_alerts={alert_cnt}"
                )
    except Exception as e:
        logger.warning(f"[attendance_cron] tick failed: {e}")
