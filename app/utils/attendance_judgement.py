"""근태 판정 순수 함수 — clock 시각을 스케줄과 대조해 라벨(early/on_time/late)을 정한다.

Attendance judgement — pure functions. No DB, no IO, no settings lookup.

**왜 저장 데이터가 아니라 판정 함수인가**
같은 한 건의 출근이 경로마다 다른 라벨을 받는 사고가 반복됐다 — 키오스크는 상수 0분,
콘솔은 설정 5분, 화면 표시는 또 다른 값. 원인은 "판정" 이 여러 파일에 복사돼 있었고
각자 임계값 출처가 달랐다는 것이다. 판정은 여기 한 곳에만 두고, 임계값은 **호출자가
매장 설정에서 읽어 주입**한다(주입 지점: `app/services/attendance_threshold_service.py`).
이 모듈이 설정을 직접 읽지 않는 이유도 같다 — 읽기(IO)와 판정(규칙)이 한 함수에 섞이면
판정 규칙을 단위 테스트로 고정할 수 없다.

**왜 분(minute) 단위인가 (R1/R2)**
clock_in/clock_out 은 초까지 저장한다(사건의 기록이므로 깎지 않는다). 그러나 **판정은
언제나 분 경계로 절삭한 값끼리** 한다. 화면이 `HH:MM` 으로 보여주는 이상, 17:00 시작
스케줄에 17:05:59 로 찍힌 출근은 화면상 `17:05` 다 — buffer 5분이면 지각이 아니어야
한다. 초를 살려 비교하면 "화면엔 정상인데 기록은 지각" 이라는, 설명할 수 없는 결과가
나온다. 절삭은 `app/utils/timezone.floor_to_minute()` 하나만 쓴다(직접 replace 금지).

경계 예 (스케줄 17:00~21:00, late_buffer=5, early_clock_in=10, early_leave=10):
    - 16:49:59 clock-in → early (사유 요구)   / 16:50:00 → on_time
    - 17:05:59 clock-in → on_time             / 17:06:00 → late
    - 20:49:59 clock-out → early leave        / 20:50:00 → on_time

**early 는 거부가 아니다.** 이 모듈은 "이르다" 는 사실만 판정한다. 사유를 받아 통과시킬지,
anomaly 로 남길지는 호출자(서비스)의 몫이며, 얼마나 일찍인지에 대한 **상한은 없다**.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.utils.timezone import floor_to_minute, minutes_between


# ─── 임계값 기본값 (설정 미등록/조회 실패 시 fallback) ────────────────────
#
# settings_registry 시드(app/seeds/settings_seed.py)의 default_value 와 **반드시**
# 같은 값을 유지한다. 갈리면 설정이 등록된 매장과 아닌 매장이 다르게 동작하고,
# 그 차이는 화면 어디에도 드러나지 않는다. 그래서 fallback 리터럴을 코드 곳곳에
# 흩어 두지 않고 여기 세 개만 둔다.
DEFAULT_LATE_BUFFER_MINUTES = 5
DEFAULT_EARLY_CLOCK_IN_THRESHOLD_MINUTES = 10
DEFAULT_EARLY_LEAVE_THRESHOLD_MINUTES = 10

# 설정 키 — 오타 시 조용히 fallback 으로 떨어지므로 상수로 고정.
SETTING_KEY_LATE_BUFFER = "attendance.late_buffer_minutes"
SETTING_KEY_EARLY_CLOCK_IN_THRESHOLD = "attendance.early_clock_in_threshold_minutes"
SETTING_KEY_EARLY_LEAVE_THRESHOLD = "attendance.early_leave_threshold_minutes"


class ClockInKind(StrEnum):
    """clock-in 판정 종류. 호출자가 status/anomaly 로 매핑한다.

    UNKNOWN: 스케줄 시각을 알 수 없음(스케줄 없음/시간 미정) — 판정하지 않는다.
             호출자는 기존 동작(working, anomaly 없음)을 유지해야 한다.
    EARLY:   예정보다 이른 출근 → 사유 요구 + `early_clock_in_override` anomaly.
    ON_TIME: 정상 출근 → status 'working'.
    LATE:    buffer 를 넘긴 지각 → status 'late' + 'late' anomaly.
    """

    UNKNOWN = "unknown"
    EARLY = "early"
    ON_TIME = "on_time"
    LATE = "late"


class ClockOutKind(StrEnum):
    """clock-out 판정 종류.

    UNKNOWN: 스케줄 종료 시각을 알 수 없음 — 판정하지 않는다.
    EARLY:   조기 퇴근 → 사유 요구 + early_clock_out / early_leave anomaly.
    ON_TIME: 정상 퇴근.
    """

    UNKNOWN = "unknown"
    EARLY = "early"
    ON_TIME = "on_time"


@dataclass(frozen=True)
class ClockInVerdict:
    """clock-in 판정 결과.

    minutes_early / minutes_late 는 **둘 다 0 이상**이며 동시에 0 이 아닐 수 없다
    (같은 분이면 둘 다 0). 사유 요구 응답의 `minutes_early`, 알림 문구, 앱 프리뷰가
    이 값을 그대로 쓴다 — 호출부마다 다시 빼기 계산을 하면 절삭 규칙이 또 갈린다.
    """

    kind: ClockInKind
    minutes_early: int
    minutes_late: int

    @property
    def is_early(self) -> bool:
        return self.kind is ClockInKind.EARLY

    @property
    def is_late(self) -> bool:
        return self.kind is ClockInKind.LATE


@dataclass(frozen=True)
class ClockOutVerdict:
    """clock-out 판정 결과. minutes_early 는 예정 종료보다 몇 분 이른지(0 이상)."""

    kind: ClockOutKind
    minutes_early: int

    @property
    def is_early(self) -> bool:
        return self.kind is ClockOutKind.EARLY


def _threshold(value: int | None) -> timedelta:
    """임계값(분) → timedelta. None/음수는 0 으로 본다.

    음수 임계값은 "정시보다 늦게 찍어야 정상" 같은 뜻이 되어 어떤 라벨도 설명할 수
    없게 된다. 설정에 잘못된 값이 들어와도 판정이 뒤집히지 않게 0 으로 막는다.
    """
    if value is None or value < 0:
        return timedelta(0)
    return timedelta(minutes=value)


def judge_clock_in(
    at: datetime | None,
    scheduled_start: datetime | None,
    *,
    late_buffer: int | None,
    early_threshold: int | None,
) -> ClockInVerdict:
    """출근 시각 판정 — early / on_time / late (분 단위, 초 버림).

    Args:
        at: 실제 clock-in 시각 (aware). 초는 판정에서 버린다.
        scheduled_start: 스케줄 시작 절대시각 (aware). None 이면 UNKNOWN.
        late_buffer: 시작 후 이 분까지는 지각 아님 (설정 attendance.late_buffer_minutes).
        early_threshold: 시작 전 이 분까지는 정상 출근
                         (설정 attendance.early_clock_in_threshold_minutes).

    Returns:
        ClockInVerdict: 판정 종류 + 몇 분 이른지/늦은지.

    early 와 late 는 동시에 성립할 수 없다 — 이르면 early 로 확정한다(elif). 예전엔
    두 판정이 각각 anomaly 리스트를 **대입**해서 나중 것이 앞의 라벨을 지웠다.
    """
    at_min = floor_to_minute(at)
    start_min = floor_to_minute(scheduled_start)
    if at_min is None or start_min is None:
        return ClockInVerdict(ClockInKind.UNKNOWN, 0, 0)

    minutes_early = minutes_between(at_min, start_min)  # start - at (음수는 0)
    minutes_late = minutes_between(start_min, at_min)   # at - start (음수는 0)

    if at_min < start_min - _threshold(early_threshold):
        kind = ClockInKind.EARLY
    elif at_min > start_min + _threshold(late_buffer):
        kind = ClockInKind.LATE
    else:
        kind = ClockInKind.ON_TIME
    return ClockInVerdict(kind, minutes_early, minutes_late)


def judge_clock_out(
    at: datetime | None,
    scheduled_end: datetime | None,
    *,
    early_leave_threshold: int | None,
) -> ClockOutVerdict:
    """퇴근 시각 판정 — early / on_time (분 단위, 초 버림).

    Args:
        at: 실제 clock-out 시각 (aware).
        scheduled_end: 스케줄 종료 절대시각 (aware). None 이면 UNKNOWN.
        early_leave_threshold: 종료 전 이 분까지는 정상 퇴근
                               (설정 attendance.early_leave_threshold_minutes).

    Returns:
        ClockOutVerdict: 판정 종류 + 예정 종료보다 몇 분 이른지.

    늦은 퇴근(초과근무)은 여기서 판정하지 않는다 — 임계값 성격이 다르고(분이 아니라
    누적 근무시간) 급여 규칙에 걸린다.
    """
    at_min = floor_to_minute(at)
    end_min = floor_to_minute(scheduled_end)
    if at_min is None or end_min is None:
        return ClockOutVerdict(ClockOutKind.UNKNOWN, 0)

    minutes_early = minutes_between(at_min, end_min)  # end - at (음수는 0)
    if at_min < end_min - _threshold(early_leave_threshold):
        return ClockOutVerdict(ClockOutKind.EARLY, minutes_early)
    return ClockOutVerdict(ClockOutKind.ON_TIME, minutes_early)


def is_late_arrival(
    at: datetime | None,
    scheduled_start: datetime | None,
    *,
    late_buffer: int | None,
) -> bool:
    """지각인지만 묻는다 — "아직 출근 안 한 상태" 판정 전용.

    미출근 승격(cron/lifecycle/화면 표시)은 early 개념이 없다: 출근 자체를 안 했으므로
    "이르다" 가 성립하지 않는다. 그런 호출부가 `judge_clock_in` 에 의미 없는
    early_threshold 를 지어내 넘기지 않도록 별도 함수로 둔다.
    """
    return judge_clock_in(
        at, scheduled_start, late_buffer=late_buffer, early_threshold=0
    ).is_late
