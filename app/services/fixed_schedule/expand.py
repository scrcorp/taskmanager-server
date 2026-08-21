"""고정 근무 패턴 펼치기(expansion) — 순수 함수, 서버 1벌.

SoT: docs/99_inbox/2026-08-20-고정근무-구현계약.md §2.

규칙 (d = 후보 날짜, operating_day)
    1. 창:        date_from ≤ d ≤ date_to
    2. 유효기간:  start_date ≤ d ≤ until_date (until_date None = 무기한)
    3. 배정 게이트: d ≤ assignable_until[user_id]
         - 값 None  = 제한 없음
         - **키 없음 = 차단** (fail-closed, staff_assignment_service._blocked 와 동일 철학)
    4. 요일:      dow_sun0(d) ∈ byday   (0=Sun..6=Sat — 파이썬 weekday 0=Mon 아님)

벽시계 조립은 기존 인코딩 유틸(`app.utils.timezone.assemble_shift_datetimes`,
`assemble_break_datetime`)을 그대로 쓴다 — overnight(end ≤ start)이면 end_at +1d,
break 가 start_time 보다 이르면 +1d. 새 조립 로직을 만들지 않는다.

억제(교체) 판정은 여기서 하지 않는다 — 호출자(read.merge_virtual)가
(pattern_id, occurrence_date) 로 실 행(deleted 포함)과 대조해 제거한다.

DB/IO 없음. `patterns` 는 StaffWorkPattern ORM 인스턴스든 같은 속성을 가진 객체든
duck-typing 으로 받는다 (unit 테스트는 SimpleNamespace 로 충분).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from app.utils.timezone import assemble_break_datetime, assemble_shift_datetimes

if TYPE_CHECKING:  # 순환 import 회피 — 런타임엔 모델을 참조하지 않는다
    from datetime import time


class PatternLike(Protocol):
    """expand 가 읽는 최소 속성 집합 (StaffWorkPattern 이 만족)."""

    id: UUID
    group_id: UUID
    user_id: UUID
    store_id: UUID
    work_role_id: UUID | None
    byday: Sequence[int]
    start_time: "time"
    end_time: "time"
    break_start_time: "time | None"
    break_end_time: "time | None"
    start_date: date
    until_date: date | None


@dataclass(frozen=True)
class Occurrence:
    """패턴 1개가 특정 날짜에 만들어 내는 근무 1건 (virtual 행의 원료)."""

    pattern_id: UUID
    group_id: UUID
    user_id: UUID
    store_id: UUID
    work_role_id: UUID | None
    occurrence_date: date  # 패턴상 날짜 = operating_day
    start_at: datetime  # naive 벽시계
    end_at: datetime  # overnight 이면 occurrence_date + 1d
    break_start_at: datetime | None
    break_end_at: datetime | None


def dow_sun0(d: date) -> int:
    """파이썬 weekday(0=Mon..6=Sun) → 0=Sun..6=Sat."""
    return (d.weekday() + 1) % 7


def _build_occurrence(p: PatternLike, d: date) -> Occurrence:
    start_at, end_at = assemble_shift_datetimes(d, p.start_time, p.end_time)
    # 모델 CHECK 가 start/end NOT NULL 을 보장하므로 None 이 될 수 없다
    assert start_at is not None and end_at is not None
    return Occurrence(
        pattern_id=p.id,
        group_id=p.group_id,
        user_id=p.user_id,
        store_id=p.store_id,
        work_role_id=p.work_role_id,
        occurrence_date=d,
        start_at=start_at,
        end_at=end_at,
        break_start_at=assemble_break_datetime(d, p.start_time, p.break_start_time),
        break_end_at=assemble_break_datetime(d, p.start_time, p.break_end_time),
    )


def expand(
    patterns: Sequence[PatternLike],
    *,
    date_from: date,
    date_to: date,
    assignable_until: Mapping[UUID, date | None],
) -> list[Occurrence]:
    """패턴들을 [date_from, date_to] 창에서 펼친다. 순수 함수.

    반환은 (occurrence_date, start_at, pattern_id) 순 정렬 — 호출자가 안정적으로
    병합·대조할 수 있게 한다. date_from > date_to 면 빈 리스트.
    """
    if date_from > date_to:
        return []

    out: list[Occurrence] = []
    for p in patterns:
        if not p.byday:
            continue
        byday = set(p.byday)

        # 패턴 유효기간 ∩ 배정 게이트 ∩ 조회 창 → 실제 순회 구간
        lo = max(date_from, p.start_date)
        hi = date_to
        if p.until_date is not None:
            hi = min(hi, p.until_date)
        if p.user_id not in assignable_until:
            continue  # fail-closed: 판정 정보 없는 사람은 펼치지 않는다
        cutoff = assignable_until[p.user_id]
        if cutoff is not None:
            hi = min(hi, cutoff)
        if lo > hi:
            continue

        d = lo
        while d <= hi:
            if dow_sun0(d) in byday:
                out.append(_build_occurrence(p, d))
            d += timedelta(days=1)

    out.sort(key=lambda o: (o.occurrence_date, o.start_at, str(o.pattern_id)))
    return out
