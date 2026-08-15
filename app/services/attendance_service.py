"""근태 관리 서비스 — 근태 및 QR 코드 비즈니스 로직.

Attendance Service — Business logic for attendance and QR code management.
Handles QR code generation, attendance scanning (clock-in/out, breaks),
admin attendance listing, and correction management.
"""

import secrets
from datetime import date, datetime, time, timedelta, timezone
from typing import Sequence
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import Attendance, AttendanceCorrection, QRCode
from app.models.attendance_break import (
    BREAK_TYPE_PAID_10MIN,
    BREAK_TYPE_UNPAID_MEAL,
    PAID_BREAK_TYPES,
    UNPAID_BREAK_TYPES,
    VALID_BREAK_TYPES,
    AttendanceBreak,
)
from app.models.organization import Store
from app.models.schedule import Schedule
from app.models.user import User
from app.repositories.attendance_repository import attendance_repository, qr_code_repository
from app.utils.attendance_judgement import (
    ClockOutKind,
    is_late_arrival,
    judge_clock_out,
)
from app.utils.exceptions import BadRequestError, NotFoundError
from app.utils.timezone import floor_to_minute, minutes_between, resolve_schedule_instants


# 지각/조퇴 임계값은 **매장 설정**에서 온다 (attendance_threshold_service).
# 예전엔 여기 LATE_BUFFER_MINUTES=0 / EARLY_LEAVE_THRESHOLD_MINUTES=5 라는 모듈 상수가
# 있었고, 화면 표시는 설정(5분)을, 실제 기록은 상수(0분)를 써서 "화면엔 정상인데 DB엔
# 지각" 이 났다. 상수를 지운 이유가 그것이니 되살리지 말 것 — 임계값은 주입받는다.
#
# no_show: cron 이 scheduled_end 이 지났는데 clock-in 이 없으면 승격 (임계값 없음).

# attendance dashboard 의 "soon" 그룹 진입 임계값 (분 단위).
# 이건 판정이 아니라 순수 표시용 그룹핑이라 설정 키가 없다.
SOON_THRESHOLD_MINUTES = 5


def compute_effective_status(
    att_status: str,
    att_clock_in: datetime | None,
    schedule_start_time: time | None,
    schedule_end_time: time | None,
    schedule_work_date: date | None,
    now: datetime,
    store_tz: ZoneInfo,
    late_buffer: int,
    soon_threshold_minutes: int = SOON_THRESHOLD_MINUTES,
    schedule_start_at: datetime | None = None,
    schedule_end_at: datetime | None = None,
) -> str:
    """attendance status + 시각 + late_buffer 로 표시용 effective status 계산.

    Pure function — DB / IO 없음. dashboard 와 identify-by-pin 등 여러 곳에서 동일 로직 재사용.

    분기:
        1. clock_in 이 이미 있음:
           - status=='late' 면 'working' 으로 승격 (출근 후 지각 마킹은 anomalies 에 별도 보존,
             effective_status 의 'late' 는 "미출근 지각" 의미로 한정 — ActionSheet 등 분기 위해)
           - 그 외 status 그대로 (no_show 강등 금지)
        2. status 가 upcoming/late 아님 (working/on_break/clocked_out 등) → 그대로
        3. schedule 정보 부족 (start_time 없음) → 그대로
        4. schedule end 지났음 → no_show
        5. 이미 late 로 기록 → late
        6. start + late_buffer 지났음 → late
        7. start - soon_threshold 이내 → soon
        8. 그 외 → upcoming
    """
    if att_clock_in is not None:
        if att_status == "late":
            return "working"
        return att_status
    if att_status not in {"upcoming", "late"}:
        return att_status
    sched_start, sched_end = resolve_schedule_instants(
        start_at=schedule_start_at, end_at=schedule_end_at,
        work_date=schedule_work_date, start_time=schedule_start_time,
        end_time=schedule_end_time, tz_name=store_tz.key,
    )
    if sched_start is None:
        return att_status
    # 판정은 분 단위로만 (초 버림) — 정시가 초 차이로 late 로 보이지 않게.
    now_min = floor_to_minute(now)
    if sched_end is not None and now_min >= sched_end:
        return "no_show"
    if att_status == "late":
        return "late"
    if is_late_arrival(now_min, sched_start, late_buffer=late_buffer):
        return "late"
    if now_min >= sched_start - timedelta(minutes=soon_threshold_minutes):
        return "soon"
    return "upcoming"


# 조기 clock-in override — 예정보다 이른 출근을 사유와 함께 강제로 찍은 기록.
ANOMALY_EARLY_CLOCK_IN_OVERRIDE = "early_clock_in_override"

# 겹쳐 출근 — 다른 근무와 시간이 겹치는 기록 (D15 로 허용된 상태).
# **파생 라벨이다.** 이 값이 있고 없고로 급여를 막지 않는다 — 이중 지급 차단은
# payroll 확정 게이트(`overlapping_attendance`)의 시간 구간 판정이 한다. 라벨은
# 화면에 드러내기 위한 것이라 낡아도 돈이 새지 않는다.
# (컬럼이 ARRAY(String(30)) 이므로 anomaly 코드는 30자 이하여야 한다.)
ANOMALY_OVERLAPPING_CLOCK_IN = "overlapping_clock_in"

# manage UI 재설계(Issue 10) 가 쓰는 anomaly 표시 후보 — server 판정 예외만.
# (soon 은 anomaly 아님 → 앱이 start_time 으로 자체 계산, 여기서 안 냄)
#
# ⚠️ 여기 등록하지 않은 코드는 HTMA today-staff/manage 응답에서 **조용히 사라진다**.
# 콘솔은 raw anomalies 를 그대로 내려서 보이므로 "콘솔엔 있는데 앱엔 없다" 는
# 재현하기 가장 어려운 불일치가 된다.
DISPLAY_ANOMALIES = (
    "late",
    "no_show",
    "early_leave",
    "overtime",
    "no_break",
    "early_clock_out",
    ANOMALY_EARLY_CLOCK_IN_OVERRIDE,
    ANOMALY_OVERLAPPING_CLOCK_IN,
)


def compute_state_and_anomalies(
    att_status: str | None,
    att_clock_in: datetime | None,
    att_clock_out: datetime | None,
    att_anomalies: list[str] | None,
    schedule_start_time: time | None,
    schedule_end_time: time | None,
    schedule_work_date: date | None,
    now: datetime,
    store_tz: ZoneInfo,
    late_buffer: int,
    schedule_start_at: datetime | None = None,
    schedule_end_at: datetime | None = None,
) -> tuple[str, list[str]]:
    """clock 이벤트 기반 state + anomaly 목록 계산 (manage UI 재설계용).

    Pure function. state 와 anomaly 를 분리한다 (clock in/out 은 이벤트일 뿐 상태 아님):
      - state: upcoming / working / breaking / done
      - anomalies: DISPLAY_ANOMALIES 중 state 와 일관된 것만

    anomaly 유효성 규칙 (출퇴근 사실과 모순되는 건 제거):
      - no_show: 미출근(clock_in 없음)에서만 + **단독** (출근 안 했으니 다른 anomaly 공존 불가)
      - overtime: clock_in 있어야 (출근해야 초과근무)
      - no_break / early_leave / early_clock_out: clock_out 있어야
        (퇴근해야 휴식없음/조기퇴근 판정 가능)
      - early_clock_in_override: clock_in 있어야 (출근해야 조기출근 강행 성립)
      - late: 미출근 지각 / 늦은 출근 모두 가능 (no_show 와만 배타)

    soon 은 여기서 내지 않는다 (앱이 자체 판단).
    """
    has_clock_in = att_clock_in is not None
    has_clock_out = att_clock_out is not None

    # state — clock 이벤트 기반
    if has_clock_out:
        state = "done"
    elif has_clock_in:
        state = "breaking" if att_status == "on_break" else "working"
    else:
        state = "upcoming"

    # 미출근 시각 판정 — no_show 면 단독 반환
    eff: str | None = None
    if not has_clock_in:
        eff = compute_effective_status(
            att_status=att_status or "upcoming",
            att_clock_in=None,
            schedule_start_time=schedule_start_time,
            schedule_end_time=schedule_end_time,
            schedule_work_date=schedule_work_date,
            now=now,
            store_tz=store_tz,
            late_buffer=late_buffer,
            schedule_start_at=schedule_start_at,
            schedule_end_at=schedule_end_at,
        )
        if eff == "no_show":
            return state, ["no_show"]

    # 저장된 anomaly 를 state 일관성으로 필터
    anomalies: list[str] = []
    for a in att_anomalies or []:
        if a not in DISPLAY_ANOMALIES:
            continue
        if a == "no_show":
            continue  # 출근 기록 있는데 no_show 면 모순 → 제거 (no_show 는 위에서 단독 처리)
        if a == "overtime" and not has_clock_in:
            continue  # 출근 안 했으면 overtime 불가
        if a == ANOMALY_EARLY_CLOCK_IN_OVERRIDE and not has_clock_in:
            continue  # 출근 안 했으면 조기출근 강행 불가
        if a == ANOMALY_OVERLAPPING_CLOCK_IN and not has_clock_in:
            continue  # 출근 기록이 없으면 겹칠 구간 자체가 없다
        if a in ("no_break", "early_leave", "early_clock_out") and not has_clock_out:
            continue  # 퇴근 안 했으면 판정 불가
        if a not in anomalies:
            anomalies.append(a)

    # 미출근 지각 — 저장된 게 없을 때 시각 계산으로 보강
    if eff == "late" and "late" not in anomalies:
        anomalies.append("late")

    return state, anomalies


async def _find_schedule_for_attendance(
    db: AsyncSession,
    user_id: UUID,
    store_id: UUID,
    work_date: date,
) -> Schedule | None:
    """해당 user/store/date의 confirmed schedule 찾기 (있으면 1개)."""
    result = await db.execute(
        select(Schedule).where(
            Schedule.user_id == user_id,
            Schedule.store_id == store_id,
            Schedule.operating_day == work_date,
            Schedule.status == "confirmed",
        ).limit(1)
    )
    return result.scalar_one_or_none()


def _add_anomaly(attendance: Attendance, code: str) -> None:
    """anomalies 배열에 중복 없이 추가."""
    current = list(attendance.anomalies or [])
    if code not in current:
        current.append(code)
    attendance.anomalies = current


# 겹침 재계산 시 함께 훑는 날짜 폭(일). 근무는 하루를 넘지 않으므로 앞뒤 하루면
# 자정을 넘는 야간조까지 짝을 찾을 수 있고, 판정 자체는 ±2일을 읽어 경계에서
# 짝이 잘려 라벨이 잘못 걷히는 일을 막는다.
_OVERLAP_REFRESH_DAYS = 1
_OVERLAP_SCAN_DAYS = 2


async def refresh_overlap_anomaly(
    db: AsyncSession,
    *,
    user_id: UUID,
    work_date: date,
    now: datetime | None = None,
) -> None:
    """`overlapping_clock_in` 라벨을 그 사람의 그 근처 날짜에 대해 다시 계산한다.

    **sticky flag 가 아니라 파생 라벨**이다 — 한쪽을 정정/취소해서 겹침이 사라지면
    라벨도 사라져야 하고, 콘솔 경로처럼 라벨을 붙이지 않는 곳에서 생긴 겹침도
    드러나야 한다. 그래서 "붙이기" 가 아니라 "다시 계산" 이다.

    커밋하지 않는다 — 호출자의 트랜잭션에 얹힌다(라벨 때문에 출퇴근이 롤백되면 안 된다).

    한계: 스캔 창(±2일) 밖의 짝은 보지 못한다. 근무 한 건이 이틀을 넘지 않는다는
    전제이며, 그 전제가 깨져도 급여는 확정 게이트가 막는다(라벨은 표시용).
    """
    if work_date is None:
        return
    now = now or datetime.now(timezone.utc)
    scan_start = work_date - timedelta(days=_OVERLAP_SCAN_DAYS)
    scan_end = work_date + timedelta(days=_OVERLAP_SCAN_DAYS)
    rows = list(
        (
            await db.execute(
                select(Attendance).where(
                    Attendance.user_id == user_id,
                    Attendance.work_date >= scan_start,
                    Attendance.work_date <= scan_end,
                    Attendance.status != "cancelled",
                    Attendance.clock_in.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return

    from app.utils.attendance_overlap import overlapping_keys_from_rows

    flagged = overlapping_keys_from_rows(
        [(a.id, a.clock_in, a.clock_out) for a in rows], now=now
    )
    write_start = work_date - timedelta(days=_OVERLAP_REFRESH_DAYS)
    write_end = work_date + timedelta(days=_OVERLAP_REFRESH_DAYS)
    for att in rows:
        # 스캔 창 가장자리 row 는 짝이 잘렸을 수 있으므로 **읽기만** 하고 쓰지 않는다.
        if not (write_start <= att.work_date <= write_end):
            continue
        current = list(att.anomalies or [])
        has = ANOMALY_OVERLAPPING_CLOCK_IN in current
        should = att.id in flagged
        if has == should:
            continue
        if should:
            current.append(ANOMALY_OVERLAPPING_CLOCK_IN)
        else:
            current = [a for a in current if a != ANOMALY_OVERLAPPING_CLOCK_IN]
        att.anomalies = current or None


# paid break 세션당 유급 인정 시간(분) — 초과분은 순 근무시간에서 차감 (결정 C1).
PAID_BREAK_PAYABLE_MINUTES = 10


def summarize_break_sessions(
    breaks: Sequence[AttendanceBreak],
) -> tuple[int, int, int, list[dict]]:
    """break 세션 목록을 paid/unpaid 집계 + paid 10분 초과분 + dict 리스트로 요약.

    Returns:
        (paid_total, unpaid_total, paid_overage, items)
        - paid_total: 모든 paid_10min(구: paid_short) 세션 duration 합
        - unpaid_total: 모든 unpaid_meal(구: unpaid_long) 세션 duration 합
        - paid_overage: 각 paid 세션별 (duration - 10, 0 하한) 합. 근무시간에서 추가 차감 대상.
        - items: 타임라인용 dict 목록

    진행 중(ended_at NULL) 세션은 집계에서 제외하고 items 에만 포함.
    """
    paid: int = 0
    unpaid: int = 0
    paid_overage: int = 0
    items: list[dict] = []
    for br in breaks:
        duration = br.duration_minutes or 0
        if br.ended_at is not None:
            if br.break_type in PAID_BREAK_TYPES:
                paid += duration
                paid_overage += max(0, duration - PAID_BREAK_PAYABLE_MINUTES)
            elif br.break_type in UNPAID_BREAK_TYPES:
                unpaid += duration
        items.append({
            "id": str(br.id),
            "started_at": br.started_at,
            "ended_at": br.ended_at,
            "break_type": br.break_type,
            "duration_minutes": br.duration_minutes,
        })
    return paid, unpaid, paid_overage, items


def compute_net_work_minutes(
    attendance: Attendance,
    breaks: Sequence[AttendanceBreak],
) -> int | None:
    """순 근무시간(분) — 결정 C1 공식의 단일 구현.

    net = max(0, total_work_minutes - unpaid break 전체 - paid break 세션별 10분 초과분)
    - unpaid break(unpaid_meal): 전체 비근무 차감
    - paid break(paid_10min): 세션당 첫 10분 유급, 초과분만 차감
    - 진행 중(ended_at NULL) break 세션은 차감하지 않음
    - total_work_minutes 가 None(clock-out 전)이면 None 반환

    build_response / get_weekly_summary / get_overtime_alerts 등 급여·집계
    로직은 반드시 이 함수를 사용한다 (공식 중복 금지, P0-1).
    """
    if attendance.total_work_minutes is None:
        return None
    _, unpaid_minutes, paid_overage, _ = summarize_break_sessions(breaks)
    return max(0, attendance.total_work_minutes - unpaid_minutes - paid_overage)


class AttendanceService:
    """근태 관리 서비스.

    Attendance service handling QR code management, attendance scanning,
    listing, and correction workflows.
    """

    # === QR 코드 관리 (QR Code Management) ===

    async def create_qr_code(
        self,
        db: AsyncSession,
        store_id: UUID,
        created_by: UUID,
    ) -> QRCode:
        """새 QR 코드를 생성합니다. 기존 활성 QR은 비활성화됩니다.

        Generate a new QR code for a store. Deactivates any existing active QR codes.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 UUID (Store UUID)
            created_by: 생성자 UUID (Creator user UUID)

        Returns:
            QRCode: 생성된 QR 코드 (Created QR code)
        """
        try:
            # 기존 활성 QR 코드 비활성화 — Deactivate existing active QR codes for the store
            await qr_code_repository.deactivate_store_qr_codes(db, store_id)

            # 새 QR 코드 생성 — Generate new random 32-char hex code
            code: str = secrets.token_hex(16)

            qr: QRCode = await qr_code_repository.create_qr(
                db,
                {
                    "store_id": store_id,
                    "code": code,
                    "is_active": True,
                    "created_by": created_by,
                },
            )

            await db.commit()
            return qr
        except Exception:
            await db.rollback()
            raise

    async def regenerate_qr_code(
        self,
        db: AsyncSession,
        qr_id: UUID,
        created_by: UUID,
    ) -> QRCode:
        """기존 QR 코드를 재생성합니다.

        Regenerate a QR code by deactivating the old one and creating a new one.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            qr_id: 기존 QR 코드 UUID (Existing QR code UUID)
            created_by: 생성자 UUID (Creator user UUID)

        Returns:
            QRCode: 새로 생성된 QR 코드 (Newly created QR code)

        Raises:
            NotFoundError: QR 코드가 없을 때 (When QR code not found)
        """
        # 기존 QR 코드 조회 — Find existing QR code
        old_qr: QRCode | None = await qr_code_repository.get_by_id(db, qr_id)
        if old_qr is None:
            raise NotFoundError("QR code not found")

        # 기존 QR 비활성화 후 새 QR 생성 — Deactivate old and create new
        # Note: create_qr_code handles commit internally
        return await self.create_qr_code(db, old_qr.store_id, created_by)

    async def get_store_qr(
        self,
        db: AsyncSession,
        store_id: UUID,
    ) -> QRCode | None:
        """매장의 활성 QR 코드를 조회합니다.

        Get the active QR code for a store.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 UUID (Store UUID)

        Returns:
            QRCode | None: 활성 QR 코드 또는 None (Active QR code or None)
        """
        return await qr_code_repository.get_qr_by_store(db, store_id)

    async def build_qr_response(
        self,
        db: AsyncSession,
        qr: QRCode,
    ) -> dict:
        """QR 코드 응답 딕셔너리를 구성합니다.

        Build QR code response dict with resolved store name.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            qr: QR 코드 ORM 객체 (QR code ORM object)

        Returns:
            dict: 매장 이름이 포함된 QR 코드 응답 (QR response with store name)
        """
        # 매장 이름 조회 — Fetch store name
        store_result = await db.execute(select(Store.name).where(Store.id == qr.store_id))
        store_name: str = store_result.scalar() or "Unknown"

        return {
            "id": str(qr.id),
            "store_id": str(qr.store_id),
            "store_name": store_name,
            "code": qr.code,
            "is_active": qr.is_active,
            "created_at": qr.created_at,
        }

    # === 근태 스캔 (Attendance Scanning) ===

    async def scan(
        self,
        db: AsyncSession,
        qr_code_str: str,
        user_id: UUID,
        organization_id: UUID,
        action: str,
        client_timezone: str | None = None,
        location: dict | None = None,
    ) -> Attendance:
        """QR 코드를 스캔하여 출퇴근/휴식을 기록합니다.

        Process a QR code scan for clock-in, break, or clock-out.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            qr_code_str: 스캔한 QR 코드 문자열 (Scanned QR code string)
            user_id: 사용자 UUID (User UUID)
            organization_id: 조직 UUID (Organization UUID)
            action: 동작 유형 — "clock_in"|"break_start"|"break_end"|"clock_out"
            client_timezone: 클라이언트 IANA 타임존 (Client IANA timezone)
            location: GPS 위치, 선택 (Optional GPS {lat, lng})

        Returns:
            Attendance: 업데이트된 근태 기록 (Updated attendance record)

        Raises:
            BadRequestError: QR 코드가 유효하지 않거나, 동작이 잘못된 경우
                             (Invalid QR code or invalid action for current state)
        """
        # QR 코드 검증 — Validate QR code exists and is active
        qr: QRCode | None = await qr_code_repository.get_qr_by_code(db, qr_code_str)
        if qr is None or not qr.is_active:
            raise BadRequestError("Invalid or inactive QR code")

        # 만료 여부 확인 — Check expiration
        if qr.expires_at is not None and datetime.now(timezone.utc) > qr.expires_at:
            raise BadRequestError("QR code has expired")

        now: datetime = datetime.now(timezone.utc)
        store_id: UUID = qr.store_id

        # 타임존 + 경계 시각 해석 — Resolve timezone and day boundary
        from app.utils.timezone import get_store_day_config, get_work_date, resolve_timezone
        store_tz, store_day_start = await get_store_day_config(db, store_id)
        effective_tz: str = resolve_timezone(client_timezone, store_tz)
        client_timezone = effective_tz
        today: date = get_work_date(effective_tz, store_day_start, now)

        # 오늘의 근태 기록 조회/생성 — Get or create today's attendance record
        attendance: Attendance | None = await attendance_repository.get_user_today(db, user_id, today)

        # 동작별 처리 — Process based on action type
        if action == "clock_in":
            # 폐점(closed) 매장엔 새 출근 기록 생성 차단 (clock_out/break 등 기존 기록 갱신은 허용)
            from app.services.store_service import store_service
            await store_service.assert_open_for_create(db, store_id)
            if attendance is not None:
                raise BadRequestError(
                    "이미 오늘 출근 기록이 있습니다 (Already clocked in today)"
                )
            # 해당 날짜의 confirmed schedule 찾기 (있으면 link + late 체크)
            schedule = await _find_schedule_for_attendance(db, user_id, store_id, today)
            new_status = "working"
            anomalies: list[str] = []
            if schedule and (schedule.start_at is not None or schedule.start_time is not None):
                # store timezone 기준 schedule 시작 순간 (start_at 우선, 없으면 combine 폴백)
                scheduled_start, _ = resolve_schedule_instants(
                    start_at=schedule.start_at, end_at=schedule.end_at,
                    work_date=schedule.work_date, start_time=schedule.start_time,
                    end_time=schedule.end_time, tz_name=effective_tz,
                )
                # late 판정은 분 단위 (clock_in 은 초까지 저장하되 초로 late 찍지 않음).
                # 임계값은 매장 설정 — QR 경로만 상수를 쓰면 키오스크와 판정이 갈린다.
                from app.services.attendance_threshold_service import resolve_late_buffer
                late_buffer = await resolve_late_buffer(
                    db, organization_id=organization_id, store_id=store_id
                )
                if is_late_arrival(now, scheduled_start, late_buffer=late_buffer):
                    new_status = "late"
                    anomalies.append("late")
            attendance = await attendance_repository.create(
                db,
                {
                    "organization_id": organization_id,
                    "store_id": store_id,
                    "user_id": user_id,
                    "schedule_id": schedule.id if schedule else None,
                    "work_date": today,
                    "clock_in": now,
                    "clock_in_timezone": client_timezone,
                    "status": new_status,
                    "anomalies": anomalies or None,
                },
            )

        elif action == "break_start":
            if attendance is None:
                raise BadRequestError(
                    "먼저 출근해야 합니다 (Must clock in first)"
                )
            if attendance.status not in ("working", "late"):
                raise BadRequestError(
                    "현재 상태에서 휴식을 시작할 수 없습니다 (Cannot start break in current state)"
                )
            attendance.break_start = now
            attendance.status = "on_break"
            await db.flush()
            await db.refresh(attendance)

        elif action == "break_end":
            if attendance is None:
                raise BadRequestError(
                    "먼저 출근해야 합니다 (Must clock in first)"
                )
            if attendance.status != "on_break":
                raise BadRequestError(
                    "현재 휴식 중이 아닙니다 (Not currently on break)"
                )
            attendance.break_end = now
            attendance.status = "working"

            # 휴식 시간 계산 — Calculate break minutes
            if attendance.break_start is not None:
                attendance.total_break_minutes = minutes_between(
                    attendance.break_start, now
                )

            await db.flush()
            await db.refresh(attendance)

        elif action == "clock_out":
            if attendance is None:
                raise BadRequestError(
                    "먼저 출근해야 합니다 (Must clock in first)"
                )
            if attendance.status not in ("working", "late", "on_break"):
                raise BadRequestError(
                    "이미 퇴근 처리되었습니다 (Already clocked out)"
                )

            # 휴식 중이면 먼저 휴식 종료 — End break if currently on break
            if attendance.status == "on_break" and attendance.break_start is not None:
                attendance.break_end = now
                attendance.total_break_minutes = minutes_between(
                    attendance.break_start, now
                )

            attendance.clock_out = now
            attendance.clock_out_timezone = client_timezone
            attendance.status = "clocked_out"

            # 총 근무 시간 계산 — Calculate total work minutes
            if attendance.clock_in is not None:
                attendance.total_work_minutes = minutes_between(
                    attendance.clock_in, now
                )

            # ─── Anomaly 감지 ───
            schedule = None
            if attendance.schedule_id is not None:
                schedule_result = await db.execute(
                    select(Schedule).where(Schedule.id == attendance.schedule_id)
                )
                schedule = schedule_result.scalar_one_or_none()

            if schedule and (schedule.end_at is not None or schedule.end_time is not None):
                scheduled_start, scheduled_end = resolve_schedule_instants(
                    start_at=schedule.start_at, end_at=schedule.end_at,
                    work_date=schedule.work_date, start_time=schedule.start_time,
                    end_time=schedule.end_time, tz_name=effective_tz,
                )
                if scheduled_end is not None:
                    # 조퇴 (분 단위 판정). 임계값은 매장 설정.
                    from app.services.attendance_threshold_service import (
                        resolve_early_leave_threshold,
                    )
                    early_leave_threshold = await resolve_early_leave_threshold(
                        db, organization_id=organization_id, store_id=store_id
                    )
                    verdict = judge_clock_out(
                        now, scheduled_end,
                        early_leave_threshold=early_leave_threshold,
                    )
                    if verdict.kind is ClockOutKind.EARLY:
                        _add_anomaly(attendance, "early_leave")
                    # 초과근무
                    if attendance.total_work_minutes is not None and scheduled_start is not None:
                        scheduled_minutes = (
                            (scheduled_end - scheduled_start).total_seconds() / 60
                        )
                        if attendance.total_work_minutes > scheduled_minutes + 30:
                            _add_anomaly(attendance, "overtime")

            # 휴식 미사용 + 연속 근무 임계치 초과 체크
            from app.repositories.break_rule_repository import break_rule_repository
            break_rule = await break_rule_repository.get_by_store(db, store_id)
            if break_rule and (attendance.total_break_minutes or 0) == 0:
                if (attendance.total_work_minutes or 0) > break_rule.max_continuous_minutes:
                    _add_anomaly(attendance, "no_break")

            await db.flush()
            await db.refresh(attendance)

        else:
            raise BadRequestError(
                f"Invalid action: {action}. Use: clock_in, break_start, break_end, clock_out"
            )

        try:
            await db.commit()
            return attendance
        except Exception:
            await db.rollback()
            raise

    # === 관리자 기능 (Admin Functions) ===

    async def get_attendances(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        user_id: UUID | None = None,
        work_date: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
        store_ids: list[UUID] | None = None,
    ) -> tuple[Sequence[Attendance], int]:
        """근태 기록 목록을 필터링하여 페이지네이션 조회합니다.

        List attendance records with filters and pagination.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            store_id: 매장 UUID 필터, 선택 (Optional store filter)
            user_id: 사용자 UUID 필터, 선택 (Optional user filter)
            work_date: 근무일 필터, 선택 (Optional date filter)
            date_from: 시작일 필터, 선택 (Optional date range start)
            date_to: 종료일 필터, 선택 (Optional date range end)
            status: 상태 필터, 선택 (Optional status filter)
            page: 페이지 번호 (Page number)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[Attendance], int]: (근태 목록, 전체 개수)
        """
        return await attendance_repository.get_by_filters(
            db, organization_id, store_id, user_id, work_date, date_from, date_to, status, page, per_page,
            store_ids=store_ids,
        )

    async def get_attendance(
        self,
        db: AsyncSession,
        attendance_id: UUID,
        organization_id: UUID,
    ) -> Attendance:
        """근태 기록 단건을 조회합니다.

        Get a single attendance record.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            attendance_id: 근태 UUID (Attendance UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            Attendance: 근태 기록 (Attendance record)

        Raises:
            NotFoundError: 근태 기록이 없을 때 (When attendance not found)
        """
        attendance: Attendance | None = await attendance_repository.get_by_id_with_org(
            db, attendance_id, organization_id
        )
        if attendance is None:
            raise NotFoundError("Attendance record not found")
        return attendance

    async def correct_attendance(
        self,
        db: AsyncSession,
        attendance_id: UUID,
        organization_id: UUID,
        field_name: str,
        corrected_value: str,
        reason: str,
        corrected_by: UUID,
    ) -> AttendanceCorrection:
        """근태 기록을 수정하고 수정 이력을 생성합니다.

        Correct an attendance field and create a correction audit record.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            attendance_id: 근태 UUID (Attendance UUID)
            organization_id: 조직 UUID (Organization UUID)
            field_name: 수정할 필드 (Field to correct)
            corrected_value: 수정 값 — ISO datetime (Corrected value)
            reason: 수정 사유 (Reason for correction)
            corrected_by: 수정자 UUID (Admin UUID)

        Returns:
            AttendanceCorrection: 생성된 수정 이력 (Created correction record)

        Raises:
            NotFoundError: 근태 기록이 없을 때 (When attendance not found)
            BadRequestError: 수정 불가 필드일 때 (When field cannot be corrected)
        """
        # 수정 가능한 필드 — 시간 정정 + 노트만.
        # Status 변경은 /attendances/{id}/actions/* (state-machine) 으로 옮겼다 —
        # 연관 필드(진행중 break, total_work_minutes 등)가 함께 일관 처리되도록.
        time_fields: set[str] = {"clock_in", "clock_out", "break_start", "break_end"}
        allowed_fields: set[str] = time_fields | {"note"}
        if field_name == "status":
            raise BadRequestError(
                "Status changes are not supported here. Use the action endpoints "
                "(/attendances/{id}/actions/clock-in, clock-out, start-break, "
                "end-break, mark-no-show, cancel, reopen)."
            )
        if field_name not in allowed_fields:
            raise BadRequestError(
                f"Cannot correct field: {field_name}. Allowed: {', '.join(allowed_fields)}"
            )

        # 근태 기록 조회 — Fetch attendance record
        attendance: Attendance = await self.get_attendance(db, attendance_id, organization_id)

        # (L3) 확정된 pay period 안의 근태는 수정 불가 — 409.
        from app.services.payroll_lock_service import ensure_not_locked
        await ensure_not_locked(
            db, store_id=attendance.store_id, work_date=attendance.work_date
        )

        # (AK-1) 시각 정정값 해석 — 저장 전에 UTC instant 로 정규화.
        # naive ISO 는 매장 타임존 벽시계로 해석한다. 이전에는 naive 값이 그대로
        # TIMESTAMPTZ 에 저장돼 UTC 로 오기록됐다 (디바이스/브라우저 로컬 시각이
        # 매장 시각인 척 들어오면 instant 가 시간 단위로 밀려 payroll 오염).
        corrected_dt: datetime | None = None
        store_tz: str | None = None
        if field_name in time_fields:
            from app.utils.timezone import get_store_timezone, interpret_clock_time

            try:
                parsed: datetime = datetime.fromisoformat(corrected_value)
            except ValueError:
                raise BadRequestError(
                    f"Invalid datetime for {field_name}: {corrected_value!r}. "
                    "Use ISO format (e.g. 2026-08-03T09:00 or with explicit offset)."
                )
            store_tz = await get_store_timezone(db, attendance.store_id)
            corrected_dt = interpret_clock_time(parsed, store_tz)
            # audit trail 도 실제 저장 instant 와 일치하도록 정규화된 값 기록
            corrected_value = corrected_dt.isoformat()

        # clock_in 없이 clock_out 만 설정 금지 — 반쪽 펀치는 근무시간이 NULL 로
        # 남아 payroll 게이트 어디에도 안 걸리고 조용히 0원이 된다 (edge 테스트 발견).
        if field_name == "clock_out" and attendance.clock_in is None:
            raise BadRequestError(
                "Cannot set clock-out on a record with no clock-in. "
                "Correct clock-in first, then clock-out."
            )

        # 기존 값 가져오기 — Get original value.
        # 비어 있었으면 NULL 이 아니라 센티널을 남긴다: "값이 없었다"도 엄연한 이전 상태다.
        from app.services import attendance_timeline as tl

        original_raw = getattr(attendance, field_name, None)
        if original_raw is None:
            original_value = tl.EMPTY if field_name == "note" else tl.NONE
        elif field_name in time_fields:
            original_value = original_raw.isoformat()
        else:
            original_value = tl.text_value(str(original_raw))

        before_status = attendance.status
        group = tl.new_group()

        # 수정 이력 생성 — Create correction record
        correction: AttendanceCorrection = await attendance_repository.create_correction(
            db,
            {
                "attendance_id": attendance_id,
                "group_id": group,
                "action": tl.ACTION_MODIFY,
                "field_name": field_name,
                "target_type": tl.TARGET_ATTENDANCE,
                "original_value": original_value,
                "corrected_value": corrected_value,
                "reason": reason,
                "corrected_by": corrected_by,
            },
        )

        # 근태 기록 업데이트 — Update attendance field with corrected value
        if field_name in time_fields:
            setattr(attendance, field_name, corrected_dt)
            # clock_in/clock_out 은 tz-name snapshot 컬럼도 함께 갱신 —
            # 저장 instant(UTC)와 해석 기준 타임존이 항상 짝을 이루도록.
            if field_name in ("clock_in", "clock_out") and store_tz is not None:
                setattr(attendance, f"{field_name}_timezone", store_tz)
        else:
            setattr(attendance, field_name, corrected_value)

        # 시간 재계산 — Recalculate minutes if relevant
        if field_name in ("clock_in", "clock_out") and attendance.clock_in and attendance.clock_out:
            attendance.total_work_minutes = minutes_between(
                attendance.clock_in, attendance.clock_out
            )

        if field_name in ("break_start", "break_end") and attendance.break_start and attendance.break_end:
            attendance.total_break_minutes = minutes_between(
                attendance.break_start, attendance.break_end
            )

        # no_show 자동 해제 — clock_in 이 채워지면 매니저가 직접 출근 인정한 것으로 간주.
        # clock_out 도 있으면 clocked_out, 없으면 working 으로 전환. anomalies 에서 no_show 제거.
        if attendance.status == "no_show" and attendance.clock_in is not None:
            attendance.status = "clocked_out" if attendance.clock_out is not None else "working"
            if attendance.anomalies:
                new_anoms = [a for a in attendance.anomalies if a != "no_show"]
                attendance.anomalies = new_anoms or None

        # clock_out 채워지면 자동 clocked_out 전환 — field 별 따로 저장되는 흐름에서
        # clock_in 만 먼저 처리되어 working 으로 갔다가 다음 clock_out correction 에서 매듭이 안 지어지는
        # 케이스 대응. on_break 였으면 break_end 가 비어있으니 손대지 않음 (운영자가 명시 처리).
        if (
            field_name == "clock_out"
            and attendance.clock_out is not None
            and attendance.status in ("working", "late")
        ):
            attendance.status = "clocked_out"

        # (L6) 자동퇴근 record 의 clock_out 을 사람이 정정하면 = 확인(confirm)으로 간주.
        # 매니저가 실제 퇴근 시각을 직접 고쳤다는 것 자체가 human-verified 이므로
        # 별도 confirm-auto-clockout 호출 없이 확인 상태를 함께 기록한다.
        # anomaly 'auto_clocked_out' 는 이력(자동퇴근이 있었다는 사실)이므로 지우지 않는다.
        self._mark_auto_clock_out_confirmed_if_applicable(
            attendance, field_name, corrected_by
        )

        # 정정의 부수효과로 status 가 바뀌었으면 그 전이도 같은 그룹에 남긴다 —
        # 값만 고쳤는데 상태가 조용히 따라 바뀌는 게 이력에 안 보이면 안 된다.
        tl.record_status(
            db,
            attendance_id=attendance_id,
            group_id=group,
            action=tl.ACTION_MODIFY,
            before=before_status,
            after=attendance.status,
            reason=reason,
            by_user_id=corrected_by,
        )

        await db.flush()

        # 시각을 고치면 겹침이 새로 생기거나 사라진다 — 라벨을 다시 계산한다.
        # (라벨은 표시용이고, 이중 지급 차단은 payroll 확정 게이트가 한다.)
        await refresh_overlap_anomaly(
            db, user_id=attendance.user_id, work_date=attendance.work_date
        )
        await db.flush()

        # 근태 수정 알림 — Notify that store's managers about attendance correction
        from app.services.alert_service import alert_service
        await alert_service.create_for_attendance_correction(
            db,
            attendance_id,
            organization_id,
            attendance.store_id,
            corrected_by,
            field_name,
        )

        try:
            await db.commit()
            return correction
        except Exception:
            await db.rollback()
            raise

    async def update_correction_reason(
        self,
        db: AsyncSession,
        *,
        attendance_id: UUID,
        correction_id: UUID,
        organization_id: UUID,
        reason: str,
    ) -> AttendanceCorrection:
        """기존 correction 의 reason 만 갱신. attendance 가 다른 org 면 NotFound.

        Update only the reason of an existing correction record.
        """
        # attendance 가 같은 org 인지 먼저 검증 (NotFound 자동 raise)
        await self.get_attendance(db, attendance_id, organization_id)

        correction = await attendance_repository.get_correction(db, correction_id)
        if correction is None or correction.attendance_id != attendance_id:
            from app.utils.exceptions import NotFoundError
            raise NotFoundError("Correction record not found")

        correction.reason = reason
        # 한 액션이 여러 행으로 쪼개져도 콘솔은 카드 하나에 사유 하나를 보여준다.
        # 한 행만 고치면 카드 안에서 사유가 갈리므로 같은 그룹 전체를 함께 갱신한다.
        if correction.group_id is not None:
            await db.execute(
                update(AttendanceCorrection)
                .where(AttendanceCorrection.group_id == correction.group_id)
                .values(reason=reason)
            )
        await db.flush()
        await db.commit()
        await db.refresh(correction)
        return correction

    # ─── 자동퇴근 확인 (L6 — payroll 마감 게이트 ①의 일상 플로우) ─────────

    @staticmethod
    def _mark_auto_clock_out_confirmed_if_applicable(
        attendance: Attendance,
        field_name: str,
        by_user_id: UUID,
    ) -> None:
        """clock_out 정정 시 auto_clocked_out record 를 확인(confirmed) 처리.

        corrected == human-verified 규칙 (L6). 이미 확인된 record 는 최초
        확인자/시각을 보존한다 (덮어쓰지 않음).
        """
        if (
            field_name == "clock_out"
            and "auto_clocked_out" in (attendance.anomalies or [])
            and attendance.auto_clock_out_confirmed_at is None
        ):
            attendance.auto_clock_out_confirmed_by = by_user_id
            attendance.auto_clock_out_confirmed_at = datetime.now(timezone.utc)

    async def confirm_auto_clock_out(
        self,
        db: AsyncSession,
        *,
        attendance_id: UUID,
        organization_id: UUID,
        confirmed_by: UUID,
    ) -> Attendance:
        """자동퇴근(auto_clocked_out) record 를 매니저가 확인(confirm) 처리.

        L6 일상 플로우 — 당일/익일 매니저·SV 가 근태 화면에서 자동퇴근 건을
        확인한다. 이 확인 상태가 payroll 마감 게이트 ①(미확인 자동퇴근 0건)의
        판정 근거다.

        규칙:
            - 'auto_clocked_out' anomaly 가 없는 record 는 400 (확인할 게 없음)
            - 이미 확인된 record 재확인은 **no-op (멱등)** — 최초 확인자/시각을
              보존한 채 200 으로 성공 반환. 콘솔 더블클릭/재시도에 안전.
            - L3 lock 가드 없음 (의도) — 확인은 시간·금액 데이터가 아니라 검증
              메타데이터라 lock 이후에도 기록 가능해야 한다 (force-confirm 케이스).

        Raises:
            NotFoundError: attendance 없음/타 org
            BadRequestError: auto_clocked_out anomaly 가 아닌 record
        """
        attendance = await self.get_attendance(db, attendance_id, organization_id)

        if "auto_clocked_out" not in (attendance.anomalies or []):
            raise BadRequestError(
                "This attendance was not auto clocked out — there is nothing to confirm."
            )

        # 멱등 no-op: 이미 확인됨 — 최초 확인 기록 보존
        if attendance.auto_clock_out_confirmed_at is not None:
            return attendance

        attendance.auto_clock_out_confirmed_by = confirmed_by
        attendance.auto_clock_out_confirmed_at = datetime.now(timezone.utc)
        await db.flush()
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        await db.refresh(attendance)
        return attendance

    # ─── 조기 출근 강행 확인 (payroll 마감 게이트 ⑥의 일상 플로우) ─────────

    async def confirm_early_clock_in(
        self,
        db: AsyncSession,
        *,
        attendance_id: UUID,
        organization_id: UUID,
        confirmed_by: UUID,
    ) -> Attendance:
        """조기 출근 강행(early_clock_in_override) 건을 매니저가 확인 처리.

        스케줄보다 이른 출근은 예정 밖 임금으로 이어지므로, 실제 clock-in 시각을
        그대로 인정하는 대신 급여 확정 전에 사람이 한 번 본다. 이 확인 상태가
        payroll 마감 게이트의 판정 근거다 (자동퇴근 확인과 동일한 계약).

        규칙:
            - override anomaly 가 없는 record 는 400 (확인할 게 없음)
            - 이미 확인된 record 재확인은 **no-op (멱등)** — 최초 확인자/시각 보존
            - 매니저 대행으로 찍힌 건은 애초에 확인된 상태로 생성된다

        Raises:
            NotFoundError: attendance 없음/타 org
            BadRequestError: override anomaly 가 아닌 record
        """
        attendance = await self.get_attendance(db, attendance_id, organization_id)

        if ANOMALY_EARLY_CLOCK_IN_OVERRIDE not in (attendance.anomalies or []):
            raise BadRequestError(
                "This attendance was not an early clock-in — there is nothing to confirm."
            )

        if attendance.early_clock_in_confirmed_at is not None:
            return attendance

        attendance.early_clock_in_confirmed_by = confirmed_by
        attendance.early_clock_in_confirmed_at = datetime.now(timezone.utc)
        await db.flush()
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        await db.refresh(attendance)
        return attendance

    async def get_corrections(
        self,
        db: AsyncSession,
        attendance_id: UUID,
    ) -> Sequence[AttendanceCorrection]:
        """근태 수정 이력을 조회합니다.

        Get correction history for an attendance record.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            attendance_id: 근태 UUID (Attendance UUID)

        Returns:
            Sequence[AttendanceCorrection]: 수정 이력 목록 (List of corrections)
        """
        return await attendance_repository.get_corrections(db, attendance_id)

    async def count_corrections_by_ids(
        self,
        db: AsyncSession,
        attendance_ids: list[UUID],
    ) -> dict[UUID, int]:
        """주어진 근태 ID 목록의 수정 이력 개수를 batch 조회합니다.

        Batch-count corrections for given attendance IDs (used by list endpoints
        to surface "edited" indicators without loading full correction rows).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            attendance_ids: 근태 UUID 목록 (Attendance UUID list)

        Returns:
            dict[UUID, int]: {attendance_id: count} — count 0 인 ID 는 키에 없음.
        """
        return await attendance_repository.count_corrections_by_attendance_ids(
            db, attendance_ids
        )

    async def _load_breaks_map(
        self,
        db: AsyncSession,
        attendance_ids: list[UUID],
    ) -> dict[UUID, list[AttendanceBreak]]:
        """여러 attendance 의 break 목록을 한 번에 로드합니다.

        Batch-load attendance_breaks rows for a list of attendance IDs.
        Returns a mapping attendance_id -> sorted list of AttendanceBreak rows.
        """
        if not attendance_ids:
            return {}
        result = await db.execute(
            select(AttendanceBreak)
            .where(AttendanceBreak.attendance_id.in_(attendance_ids))
            .order_by(AttendanceBreak.started_at.asc())
        )
        out: dict[UUID, list[AttendanceBreak]] = {}
        for br in result.scalars().all():
            out.setdefault(br.attendance_id, []).append(br)
        return out

    async def build_response(
        self,
        db: AsyncSession,
        attendance: Attendance,
        breaks: list[AttendanceBreak] | None = None,
        late_buffer: int | None = None,
    ) -> dict:
        """근태 응답 딕셔너리를 구성합니다 (관련 엔티티 이름 포함).

        Build attendance response dict with resolved entity names.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            attendance: 근태 ORM 객체 (Attendance ORM object)
            breaks: 미리 로드된 break 목록, 선택 (Preloaded attendance_breaks for batching)
            late_buffer: 지각 유예 분. None 이면 여기서 매장 설정을 조회한다.
                **목록 루프에서는 반드시 주입할 것** — resolve_setting 은 호출마다
                registry+org(+store) 를 조회하므로 레코드마다 부르면 N+1 이 된다.

        Returns:
            dict: 매장/사용자 이름이 포함된 응답 딕셔너리
                  (Response dict with store/user names)
        """
        if late_buffer is None:
            from app.services.attendance_threshold_service import resolve_late_buffer
            late_buffer = await resolve_late_buffer(
                db,
                organization_id=attendance.organization_id,
                store_id=attendance.store_id,
            )
        # 매장 이름 + 타임존 조회 — Fetch store name & timezone
        store_result = await db.execute(
            select(Store.name, Store.timezone).where(Store.id == attendance.store_id)
        )
        store_row = store_result.one_or_none()
        store_name: str = (store_row[0] if store_row else None) or "Unknown"
        store_tz_name: str | None = store_row[1] if store_row else None

        # 사용자 이름 조회 — Fetch user name
        user_result = await db.execute(select(User.full_name).where(User.id == attendance.user_id))
        user_name: str = user_result.scalar() or "Unknown"

        # 연결된 스케줄 조회 (있으면) — scheduled_start/end + effective_status 계산용 raw 값
        scheduled_start: datetime | None = None
        scheduled_end: datetime | None = None
        s_operating_day = None
        s_start_at = None
        s_end_at = None
        if attendance.schedule_id is not None:
            sch_result = await db.execute(
                select(Schedule.operating_day, Schedule.start_at, Schedule.end_at)
                .where(Schedule.id == attendance.schedule_id)
            )
            sch_row = sch_result.one_or_none()
            if sch_row is not None:
                s_operating_day, s_start_at, s_end_at = sch_row
                # store.timezone이 None이면 organization timezone 조회 (fallback)
                if store_tz_name is None:
                    from app.models.organization import Organization
                    org_result = await db.execute(
                        select(Organization.timezone)
                        .join(Store, Store.organization_id == Organization.id)
                        .where(Store.id == attendance.store_id)
                    )
                    store_tz_name = org_result.scalar() or "UTC"
                try:
                    from zoneinfo import ZoneInfo
                    tz = ZoneInfo(store_tz_name)
                except Exception:
                    from zoneinfo import ZoneInfo
                    tz = ZoneInfo("UTC")
                scheduled_start, scheduled_end = resolve_schedule_instants(
                    start_at=s_start_at, end_at=s_end_at, work_date=s_operating_day,
                    start_time=None, end_time=None, tz_name=tz.key,
                )

        # break 로드 (미리 주입되지 않았다면 fetch) — Load breaks if not provided
        if breaks is None:
            br_result = await db.execute(
                select(AttendanceBreak)
                .where(AttendanceBreak.attendance_id == attendance.id)
                .order_by(AttendanceBreak.started_at.asc())
            )
            breaks = list(br_result.scalars().all())

        paid_break_minutes, unpaid_break_minutes, paid_overage_minutes, break_items = (
            summarize_break_sessions(breaks)
        )

        # store tz 기준 "HH:MM" display formatter — admin UI 가 브라우저 로컬 tz 변환
        # 없이 그대로 렌더할 수 있도록 pre-formatted 값 제공.
        # store.timezone 이 아직 없으면 organization tz, 그마저도 없으면 UTC fallback.
        display_tz_name: str | None = store_tz_name
        if display_tz_name is None:
            from app.models.organization import Organization
            org_result = await db.execute(
                select(Organization.timezone)
                .join(Store, Store.organization_id == Organization.id)
                .where(Store.id == attendance.store_id)
            )
            display_tz_name = org_result.scalar() or "UTC"
        try:
            from zoneinfo import ZoneInfo
            display_tz = ZoneInfo(display_tz_name)
        except Exception:
            from zoneinfo import ZoneInfo
            display_tz = ZoneInfo("UTC")

        def _display_store_tz(value: datetime | None) -> str | None:
            """UTC/offset-aware datetime → store tz 기준 HH:MM. None → None."""
            if value is None:
                return None
            try:
                return value.astimezone(display_tz).strftime("%H:%M")
            except Exception:
                return None

        # break 타임라인에 started_at/ended_at_display 추가
        for item in break_items:
            item["started_at_display"] = _display_store_tz(item.get("started_at"))
            item["ended_at_display"] = _display_store_tz(item.get("ended_at"))

        # 총 휴식시간 — prefer aggregated paid+unpaid (더 정확), fallback to legacy field
        aggregated_break_minutes: int | None = None
        if break_items:
            aggregated_break_minutes = paid_break_minutes + unpaid_break_minutes
        total_break_minutes: int | None = (
            aggregated_break_minutes if aggregated_break_minutes is not None
            else attendance.total_break_minutes
        )

        # 순 근무시간 — 결정 C1 단일 공식 (compute_net_work_minutes 공용)
        net_work_minutes: int | None = compute_net_work_minutes(attendance, breaks)

        return {
            "id": str(attendance.id),
            "store_id": str(attendance.store_id),
            "store_name": store_name,
            "user_id": str(attendance.user_id),
            "user_name": user_name,
            "schedule_id": str(attendance.schedule_id) if attendance.schedule_id else None,
            "work_date": attendance.work_date,
            "clock_in": attendance.clock_in,
            "clock_in_display": _display_store_tz(attendance.clock_in),
            "clock_in_timezone": attendance.clock_in_timezone,
            "break_start": attendance.break_start,
            "break_end": attendance.break_end,
            "clock_out": attendance.clock_out,
            "clock_out_display": _display_store_tz(attendance.clock_out),
            "clock_out_timezone": attendance.clock_out_timezone,
            "scheduled_start": scheduled_start,
            "scheduled_start_display": _display_store_tz(scheduled_start),
            "scheduled_end": scheduled_end,
            "scheduled_end_display": _display_store_tz(scheduled_end),
            "status": attendance.status,
            "effective_status": compute_effective_status(
                att_status=attendance.status,
                att_clock_in=attendance.clock_in,
                schedule_start_time=s_start_at.time() if s_start_at else None,
                schedule_end_time=s_end_at.time() if s_end_at else None,
                schedule_work_date=s_operating_day,
                now=datetime.now(timezone.utc),
                store_tz=display_tz,
                late_buffer=late_buffer,
                schedule_start_at=s_start_at,
                schedule_end_at=s_end_at,
            ),
            "anomalies": attendance.anomalies,
            "total_work_minutes": attendance.total_work_minutes,
            "total_break_minutes": total_break_minutes,
            "paid_break_minutes": paid_break_minutes,
            "unpaid_break_minutes": unpaid_break_minutes,
            "paid_break_overage_minutes": paid_overage_minutes,
            "net_work_minutes": net_work_minutes,
            "breaks": break_items,
            "note": attendance.note,
            # L6 — 자동퇴근 확인 상태 (콘솔 미확인 배지용). 미확인이면 둘 다 None.
            "auto_clock_out_confirmed_at": attendance.auto_clock_out_confirmed_at,
            "auto_clock_out_confirmed_by": (
                str(attendance.auto_clock_out_confirmed_by)
                if attendance.auto_clock_out_confirmed_by is not None else None
            ),
            # 조기 출근 강행 확인 상태 (콘솔 미확인 배지용). 미확인이면 둘 다 None.
            "early_clock_in_confirmed_at": attendance.early_clock_in_confirmed_at,
            "early_clock_in_confirmed_by": (
                str(attendance.early_clock_in_confirmed_by)
                if attendance.early_clock_in_confirmed_by is not None else None
            ),
            # 조기 출근 요청자(D9) — **표시용이 아니다.** 화면 문구는 사유 문자열이
            # 담당하고, 이 값은 이름 해석·집계를 나중에 할 수 있게 남겨두는 식별자다.
            "early_clock_in_requested_by": (
                str(attendance.early_clock_in_requested_by)
                if attendance.early_clock_in_requested_by is not None else None
            ),
            "created_at": attendance.created_at,
        }

    async def build_correction_response(
        self,
        db: AsyncSession,
        correction: AttendanceCorrection,
    ) -> dict:
        """수정 이력 응답 딕셔너리를 구성합니다.

        Build correction response dict with resolved corrector name.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            correction: 수정 이력 ORM 객체 (Correction ORM object)

        Returns:
            dict: 수정자 이름이 포함된 응답 딕셔너리
                  (Response dict with corrector name)
        """
        # 수정자 이름 조회 — Fetch corrector name. NULL = system actor (cron).
        corrected_by_name: str
        if correction.corrected_by is None:
            corrected_by_name = "System"
        else:
            user_result = await db.execute(
                select(User.full_name).where(User.id == correction.corrected_by)
            )
            corrected_by_name = user_result.scalar() or "Unknown"

        return {
            "id": str(correction.id),
            "group_id": str(correction.group_id) if correction.group_id else None,
            "action": correction.action,
            "field_name": correction.field_name,
            "target_type": correction.target_type,
            "target_id": str(correction.target_id) if correction.target_id else None,
            "original_value": correction.original_value,
            "corrected_value": correction.corrected_value,
            "reason": correction.reason,
            "corrected_by": str(correction.corrected_by) if correction.corrected_by else None,
            "corrected_by_name": corrected_by_name,
            "created_at": correction.created_at,
        }


    async def get_weekly_summary(
        self,
        db: AsyncSession,
        organization_id: UUID,
        user_id: UUID | None = None,
        store_id: UUID | None = None,
        week_date: date | None = None,
        store_ids: list[UUID] | None = None,
    ) -> list[dict]:
        """주간 근무시간 요약 — 사용자별 일일/주간 근무시간.

        Weekly work time summary — per-user daily and weekly totals.
        net_work_minutes 는 attendance 별 C1 공식(compute_net_work_minutes)의 합 —
        gross - 전체 break 가 아니라, unpaid 전체 + paid 10분 초과분만 차감 (P0-1).
        """
        import datetime as dt

        target_date = week_date or date.today()
        weekday = target_date.weekday()
        week_start = target_date - dt.timedelta(days=(weekday + 1) % 7)
        week_end = week_start + dt.timedelta(days=6)

        query = (
            select(Attendance)
            .where(
                Attendance.organization_id == organization_id,
                Attendance.work_date >= week_start,
                Attendance.work_date <= week_end,
            )
        )
        if user_id:
            query = query.where(Attendance.user_id == user_id)
        if store_id:
            query = query.where(Attendance.store_id == store_id)
        if store_ids is not None:
            query = query.where(Attendance.store_id.in_(store_ids))

        result = await db.execute(query)
        attendances = list(result.scalars().all())

        # break 세션 일괄 로드 — attendance 별 C1 net 계산용
        breaks_map = await self._load_breaks_map(db, [a.id for a in attendances])

        # 사용자별 집계 — net 은 per-attendance C1 net 의 합
        aggregates: dict[UUID, dict] = {}
        for att in attendances:
            agg = aggregates.setdefault(att.user_id, {
                "total_work": 0, "total_break": 0, "net": 0, "days": 0,
            })
            agg["days"] += 1
            agg["total_work"] += att.total_work_minutes or 0
            agg["total_break"] += att.total_break_minutes or 0
            net = compute_net_work_minutes(att, breaks_map.get(att.id, []))
            agg["net"] += net or 0

        # 사용자 이름 일괄 조회 — Batch load user names
        names_result = await db.execute(
            select(User.id, User.full_name).where(User.id.in_(list(aggregates.keys())))
        )
        names_map = {row.id: row.full_name for row in names_result}

        summaries: list[dict] = []
        for uid, agg in aggregates.items():
            summaries.append({
                "user_id": str(uid),
                "user_name": names_map.get(uid) or "Unknown",
                "week_start": str(week_start),
                "week_end": str(week_end),
                "days_worked": agg["days"],
                "total_work_minutes": agg["total_work"],
                "total_break_minutes": agg["total_break"],
                "net_work_minutes": agg["net"],
                "net_work_hours": round(agg["net"] / 60, 1),
            })
        return summaries

    async def get_overtime_alerts(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        week_date: date | None = None,
        store_ids: list[UUID] | None = None,
    ) -> list[dict]:
        """주간 초과근무 경고 목록 조회.

        Get overtime alerts — users whose weekly NET hours exceed threshold.
        - 주간 시간은 attendance 별 C1 net(compute_net_work_minutes) 합산 (P0-2)
        - 기준(max weekly)은 매장별 LaborLawSetting cascade — resolve_weekly_max_hours (P0-3)
          한 주에 여러 매장 근무 시 가장 엄격한(작은) 기준으로 판정.
        """
        import datetime as dt
        from app.services.labor_law_service import (
            DEFAULT_MAX_WEEKLY_HOURS,
            resolve_weekly_max_hours,
        )

        target_date = week_date or date.today()
        weekday = target_date.weekday()
        week_start = target_date - dt.timedelta(days=(weekday + 1) % 7)
        week_end = week_start + dt.timedelta(days=6)

        # 주간 attendance rows 조회 — C1 net 계산에 break 세션 필요, 집계 SQL 대신 row 로드
        query = (
            select(Attendance)
            .where(
                Attendance.organization_id == organization_id,
                Attendance.work_date >= week_start,
                Attendance.work_date <= week_end,
            )
        )
        if store_id:
            query = query.where(Attendance.store_id == store_id)
        if store_ids is not None:
            query = query.where(Attendance.store_id.in_(store_ids))

        result = await db.execute(query)
        attendances = list(result.scalars().all())

        breaks_map = await self._load_breaks_map(db, [a.id for a in attendances])

        # 사용자별 주간 net 합산 + 근무 매장 수집
        net_by_user: dict[UUID, int] = {}
        stores_by_user: dict[UUID, set[UUID]] = {}
        for att in attendances:
            net = compute_net_work_minutes(att, breaks_map.get(att.id, [])) or 0
            net_by_user[att.user_id] = net_by_user.get(att.user_id, 0) + net
            if att.store_id is not None:
                stores_by_user.setdefault(att.user_id, set()).add(att.store_id)

        # 매장별 주간 최대시간 resolve — 매장당 1회 조회
        involved_store_ids = {sid for sids in stores_by_user.values() for sid in sids}
        max_weekly_by_store: dict[UUID, int] = {}
        for sid in involved_store_ids:
            max_weekly_by_store[sid] = await resolve_weekly_max_hours(db, sid)

        alerts: list[dict] = []
        for uid, net_minutes in net_by_user.items():
            user_store_ids = stores_by_user.get(uid)
            if user_store_ids:
                # 여러 매장 근무 주간이면 가장 엄격한(작은) 기준 적용 — 보수적 판정
                max_weekly = min(max_weekly_by_store[sid] for sid in user_store_ids)
            else:
                # store 유실(SET NULL) row 만 있는 경우 — 기본값으로 판정
                max_weekly = DEFAULT_MAX_WEEKLY_HOURS
            total_hours = net_minutes / 60
            if total_hours > max_weekly:
                user_result = await db.execute(
                    select(User.full_name).where(User.id == uid)
                )
                user_name = user_result.scalar() or "Unknown"
                alerts.append({
                    "user_id": str(uid),
                    "user_name": user_name,
                    "week_start": str(week_start),
                    "week_end": str(week_end),
                    "total_hours": round(total_hours, 1),
                    "max_weekly_hours": max_weekly,
                    "overtime_hours": round(total_hours - max_weekly, 1),
                })
        return alerts

    # ─── Break session CRUD (admin correction) ──────────────────────────────
    # admin 이 attendance_breaks row 를 직접 추가/수정/삭제할 수 있게 한다.
    # 동시성 보장: 새 세션 추가/수정 시 이미 열린(open) break 와 시간이 겹치지 않는지 검증.

    async def _recalculate_break_aggregates(
        self,
        db: AsyncSession,
        attendance: Attendance,
    ) -> None:
        """attendance.total_break_minutes 를 attendance_breaks 합계로 재계산."""
        result = await db.execute(
            select(AttendanceBreak)
            .where(AttendanceBreak.attendance_id == attendance.id)
        )
        breaks = list(result.scalars().all())
        total: int = sum((b.duration_minutes or 0) for b in breaks if b.ended_at is not None)
        attendance.total_break_minutes = total

    @staticmethod
    def _validate_break_session(
        started_at: datetime,
        ended_at: datetime | None,
        break_type: str,
    ) -> None:
        """break_type 화이트리스트 + started_at < ended_at 검증."""
        if break_type not in VALID_BREAK_TYPES:
            raise BadRequestError(
                f"Invalid break_type: {break_type}. Allowed: {', '.join(sorted(VALID_BREAK_TYPES))}"
            )
        if ended_at is not None and ended_at <= started_at:
            raise BadRequestError("ended_at must be after started_at")

    @staticmethod
    def _check_overlap(
        sessions: list[AttendanceBreak],
        started_at: datetime,
        ended_at: datetime | None,
        exclude_id: UUID | None = None,
    ) -> None:
        """기존 세션 목록과 시간 구간 겹침 검증.

        ended_at 이 None (open) 이면 시작 시각만으로 검사 — 다른 세션의 구간 안에 들어가면 안됨.
        그 외에는 (start, end) 두 구간이 겹치면 안됨.
        """
        for s in sessions:
            if exclude_id is not None and s.id == exclude_id:
                continue
            s_start = s.started_at
            s_end = s.ended_at
            # 기존 세션이 open 이면 (s_end is None) 무한대로 가정
            new_end = ended_at
            # 두 구간 [a_start, a_end), [b_start, b_end) 가 겹치는지
            # — None 은 +infinity 로 간주
            a_start, a_end = started_at, new_end
            b_start, b_end = s_start, s_end
            overlap = (
                (a_end is None or a_end > b_start)
                and (b_end is None or b_end > a_start)
            )
            if overlap:
                raise BadRequestError(
                    "Break session overlaps an existing one"
                )

    async def add_break(
        self,
        db: AsyncSession,
        attendance_id: UUID,
        organization_id: UUID,
        started_at: datetime,
        ended_at: datetime | None,
        break_type: str,
        reason: str | None = None,
        by_user_id: UUID | None = None,
    ) -> AttendanceBreak:
        """admin 이 새 break 세션을 attendance 에 추가."""
        attendance = await self.get_attendance(db, attendance_id, organization_id)

        # naive 입력은 store 벽시계로 해석 (P0-5와 동일 규칙) — 미정규화 시
        # 기존 aware 세션과의 겹침 비교에서 TypeError(500)가 났다.
        from app.utils.timezone import get_store_timezone, interpret_clock_time
        store_tz = await get_store_timezone(db, attendance.store_id)
        started_at = interpret_clock_time(started_at, store_tz)
        if ended_at is not None:
            ended_at = interpret_clock_time(ended_at, store_tz)

        self._validate_break_session(started_at, ended_at, break_type)
        from app.models.attendance_break import normalize_break_type
        break_type = normalize_break_type(break_type)

        # (L3) 확정된 pay period 안의 근태는 break 추가 불가 — 409.
        from app.services.payroll_lock_service import ensure_not_locked
        await ensure_not_locked(
            db, store_id=attendance.store_id, work_date=attendance.work_date
        )

        # 기존 세션 로드 + 겹침 검증
        existing = await db.execute(
            select(AttendanceBreak).where(AttendanceBreak.attendance_id == attendance_id)
        )
        sessions = list(existing.scalars().all())
        self._check_overlap(sessions, started_at, ended_at)

        duration: int | None = None
        if ended_at is not None:
            duration = minutes_between(started_at, ended_at)

        new_break = AttendanceBreak(
            attendance_id=attendance_id,
            started_at=started_at,
            ended_at=ended_at,
            break_type=break_type,
            duration_minutes=duration,
        )
        db.add(new_break)
        await db.flush()

        await self._recalculate_break_aggregates(db, attendance)

        # 휴식 세션 추가 이력 — 이전엔 이 경로가 이력을 아무것도 남기지 않아
        # 콘솔에서 휴식을 고쳐도 Activity History 에 흔적이 없었다.
        from app.services import attendance_timeline as tl
        tl.record_break_snapshot(
            db,
            attendance_id=attendance.id,
            group_id=tl.new_group(),
            action=tl.ACTION_BREAK_ADDED,
            break_id=new_break.id,
            before=(None, None, None),
            after=(new_break.started_at, new_break.ended_at, new_break.break_type),
            reason=reason,
            by_user_id=by_user_id,
        )
        await db.flush()

        try:
            await db.commit()
            await db.refresh(new_break)
            return new_break
        except Exception:
            await db.rollback()
            raise

    async def update_break(
        self,
        db: AsyncSession,
        attendance_id: UUID,
        break_id: UUID,
        organization_id: UUID,
        started_at: datetime | None,
        ended_at: datetime | None,
        break_type: str | None,
        clear_ended_at: bool,
        reason: str | None = None,
        by_user_id: UUID | None = None,
    ) -> AttendanceBreak:
        """admin 이 기존 break 세션을 수정.

        Args:
            clear_ended_at: True 면 ended_at 을 명시적으로 None 으로 설정 (다시 open).
                            False 면 ended_at 인자 그대로 (None 인자는 "변경 안 함").
        """
        attendance = await self.get_attendance(db, attendance_id, organization_id)

        # (L3) 확정된 pay period 안의 근태는 break 수정 불가 — 409.
        from app.services.payroll_lock_service import ensure_not_locked
        await ensure_not_locked(
            db, store_id=attendance.store_id, work_date=attendance.work_date
        )

        target = await db.scalar(
            select(AttendanceBreak)
            .where(AttendanceBreak.id == break_id, AttendanceBreak.attendance_id == attendance_id)
        )
        if target is None:
            raise NotFoundError("Break session not found")

        # naive 입력은 store 벽시계로 해석 (add_break와 동일)
        from app.utils.timezone import get_store_timezone, interpret_clock_time
        store_tz = await get_store_timezone(db, attendance.store_id)
        if started_at is not None:
            started_at = interpret_clock_time(started_at, store_tz)
        if ended_at is not None:
            ended_at = interpret_clock_time(ended_at, store_tz)

        new_started = started_at if started_at is not None else target.started_at
        if clear_ended_at:
            new_ended: datetime | None = None
        else:
            new_ended = ended_at if ended_at is not None else target.ended_at
        new_type = break_type if break_type is not None else target.break_type

        self._validate_break_session(new_started, new_ended, new_type)
        from app.models.attendance_break import normalize_break_type
        new_type = normalize_break_type(new_type)

        # 자기 자신 제외하고 겹침 검사
        existing = await db.execute(
            select(AttendanceBreak).where(AttendanceBreak.attendance_id == attendance_id)
        )
        sessions = list(existing.scalars().all())
        self._check_overlap(sessions, new_started, new_ended, exclude_id=break_id)

        # 변경 전 스냅샷 — 이력의 before 로 쓴다.
        before_snapshot = (target.started_at, target.ended_at, target.break_type)

        target.started_at = new_started
        target.ended_at = new_ended
        target.break_type = new_type
        target.duration_minutes = (
            None if new_ended is None
            else minutes_between(new_started, new_ended)
        )
        await db.flush()

        await self._recalculate_break_aggregates(db, attendance)

        from app.services import attendance_timeline as tl
        tl.record_break_snapshot(
            db,
            attendance_id=attendance.id,
            group_id=tl.new_group(),
            action=tl.ACTION_BREAK_UPDATED,
            break_id=target.id,
            before=before_snapshot,
            after=(target.started_at, target.ended_at, target.break_type),
            reason=reason,
            by_user_id=by_user_id,
        )
        await db.flush()

        try:
            await db.commit()
            await db.refresh(target)
            return target
        except Exception:
            await db.rollback()
            raise

    async def delete_break(
        self,
        db: AsyncSession,
        attendance_id: UUID,
        break_id: UUID,
        organization_id: UUID,
        reason: str | None = None,
        by_user_id: UUID | None = None,
    ) -> None:
        """admin 이 break 세션을 삭제. attendance 의 누적 분 재계산."""
        attendance = await self.get_attendance(db, attendance_id, organization_id)

        # (L3) 확정된 pay period 안의 근태는 break 삭제 불가 — 409.
        from app.services.payroll_lock_service import ensure_not_locked
        await ensure_not_locked(
            db, store_id=attendance.store_id, work_date=attendance.work_date
        )

        target = await db.scalar(
            select(AttendanceBreak)
            .where(AttendanceBreak.id == break_id, AttendanceBreak.attendance_id == attendance_id)
        )
        if target is None:
            raise NotFoundError("Break session not found")

        # 삭제 전 스냅샷 — 세션 행은 사라져도 "무엇을 지웠는지"는 남아야 한다.
        # target_id 에 FK 를 걸지 않은 이유가 이것이다.
        before_snapshot = (target.started_at, target.ended_at, target.break_type)
        deleted_break_id = target.id

        await db.delete(target)
        await db.flush()

        await self._recalculate_break_aggregates(db, attendance)

        from app.services import attendance_timeline as tl
        tl.record_break_snapshot(
            db,
            attendance_id=attendance.id,
            group_id=tl.new_group(),
            action=tl.ACTION_BREAK_REMOVED,
            break_id=deleted_break_id,
            before=before_snapshot,
            after=(None, None, None),
            reason=reason,
            by_user_id=by_user_id,
        )
        await db.flush()

        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise


# 싱글턴 인스턴스 — Singleton instance
attendance_service: AttendanceService = AttendanceService()
