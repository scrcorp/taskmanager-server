"""근무 구간 겹침 판정 — 순수 함수. DB/IO 없음.

Attendance overlap detection — pure interval math.

**왜 anomaly 플래그가 아니라 시간 구간인가**
같은 사람의 두 근무가 시간상 겹치면 같은 시간이 두 번 급여로 나간다. 그런데 겹침을
만드는 경로는 키오스크 하나가 아니다 — 콘솔 `attendance_action_service.clock_in()` 에는
겹침 가드가 아예 없어서, 매니저가 두 shift 를 각각 clock-in 시키면 라벨 하나 없이
겹침이 만들어진다. 라벨(anomaly)로 판정하면 그 경로에서 생긴 겹침을 영영 못 잡는다.
그래서 **차단(급여 확정 게이트)은 언제나 이 모듈의 구간 판정**이 하고, anomaly
`overlapping_clock_in` 은 화면에 드러내기 위한 파생 표시일 뿐이다. 라벨이 낡아도
급여는 새지 않는다.

**경계 규칙**
구간은 분 절삭한 `[clock_in, clock_out]` 이고 **교집합이 1분 이상**일 때만 겹침이다.
초를 살려 비교하면 화면상 09:00 종료 / 09:00 시작인 두 shift 가 초 차이로 겹침이 되어,
사람이 볼 수 없는 이유로 급여 확정이 막힌다. 맞닿은 구간(한쪽 종료 == 다른 쪽 시작)은
겹침이 아니다.

**아직 안 닫힌 근무의 끝** — `now` 가 아니라 **`now` 가 속한 분의 끝**(= 절삭 후 +1분)
이다. `now` 로 자르면 두 근무가 같은 분에 겹쳐 시작한 순간 양쪽 구간이 길이 0 이 되어
"방금 두 shift 에 동시에 출근했다" 는, 이 판정이 존재하는 바로 그 상황이 겹침으로
잡히지 않는다(라벨이 clock-in 시점에 절대 안 붙는다). 아직 열려 있는 근무는 적어도
지금 이 분을 점유하고 있으므로 그 사실을 구간에 담는다. 이 규칙은 오탐을 만들지
않는다 — 이미 닫힌 근무는 그대로이고, 09:00 에 닫힌 근무와 09:00 에 시작한 열린 근무는
여전히 맞닿기만 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Hashable, Iterable, Sequence

from app.utils.timezone import floor_to_minute


@dataclass(frozen=True)
class OverlapInterval:
    """겹침 판정 대상 구간 1건.

    key: 호출자가 결과를 되찾는 식별자(보통 attendance.id).
    start/end: 이미 분 절삭된 aware datetime. `build_interval()` 로 만들 것.
    """

    key: Hashable
    start: datetime
    end: datetime


def build_interval(
    key: Hashable,
    clock_in: datetime | None,
    clock_out: datetime | None,
    *,
    now: datetime,
) -> OverlapInterval | None:
    """clock_in/out → 판정 구간. clock_in 이 없으면 구간 자체가 없다(None).

    아직 안 닫힌 근무는 **지금 이 분의 끝**까지 열려 있는 것으로 본다 — "지금 두
    shift 에 동시에 출근해 있다" 가 정확히 이 케이스이고, 그게 겹침을 막아야 하는
    첫 번째 이유다. `now` 에서 끊으면 방금 찍은 근무의 구간이 길이 0 이 되어
    그 상황이 영영 안 잡힌다(모듈 docstring 참조).
    """
    start = floor_to_minute(clock_in)
    if start is None:
        return None
    closed = floor_to_minute(clock_out)
    if closed is not None:
        end = closed
    else:
        open_end = floor_to_minute(now)
        if open_end is None:  # pragma: no cover - now 는 항상 값이 있다
            return None
        end = open_end + timedelta(minutes=1)
    if end < start:
        # 정정 실수로 뒤집힌 기록 — 길이 0 으로 보되 버리지는 않는다.
        end = start
    return OverlapInterval(key=key, start=start, end=end)


def intervals_overlap(a: OverlapInterval, b: OverlapInterval) -> bool:
    """두 구간의 교집합이 1분 이상인가. 맞닿기만 한 구간은 겹침이 아니다."""
    return max(a.start, b.start) < min(a.end, b.end)


def overlapping_keys(intervals: Iterable[OverlapInterval]) -> set[Hashable]:
    """서로 겹치는 구간들의 key 집합 — **겹치는 쌍의 양쪽 모두** 포함.

    한쪽만 표시하면 "어느 화면에서 봐도 드러난다" 는 목적을 잃는다. 매니저는 둘 중
    아무 쪽 상세를 먼저 열지 알 수 없기 때문이다.
    """
    items: list[OverlapInterval] = sorted(intervals, key=lambda i: (i.start, i.end))
    flagged: set[Hashable] = set()
    active: list[OverlapInterval] = []
    for current in items:
        # 이미 끝난 구간은 후보에서 뺀다 (start 오름차순이라 다시 겹칠 일이 없다).
        active = [a for a in active if a.end > current.start]
        for other in active:
            if intervals_overlap(current, other):
                flagged.add(current.key)
                flagged.add(other.key)
        active.append(current)
    return flagged


def overlapping_keys_from_rows(
    rows: Sequence[tuple[Hashable, datetime | None, datetime | None]],
    *,
    now: datetime,
) -> set[Hashable]:
    """(key, clock_in, clock_out) 튜플 목록에서 바로 겹침 key 를 구한다.

    호출부마다 build_interval 루프를 다시 쓰지 않게 하기 위한 얇은 래퍼 — 판정 규칙이
    복사되는 순간 경로별로 결과가 갈린다.
    """
    built = [
        interval
        for key, clock_in, clock_out in rows
        if (interval := build_interval(key, clock_in, clock_out, now=now)) is not None
    ]
    return overlapping_keys(built)
