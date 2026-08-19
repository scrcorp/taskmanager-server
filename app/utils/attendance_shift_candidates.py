"""clock-in 대상 shift 후보 규칙 — 순수 함수. DB/IO 없음.

Shift candidate rules for clock-in — pure. No DB, no IO, no settings lookup.

**왜 이 모듈이 따로 있는가 (원인 A 의 근본 수정)**
같은 질문("이 사람이 지금 찍으면 어느 shift 냐")에 두 경로가 각자 다르게 답하고
있었다. 키오스크 clock-in 은 Schedule 을 조회해 *"가장 가까운 미래"* 를 골랐고,
identify(PIN 식별)는 Attendance 를 조회해 status 랭크(`upcoming` 4 < `no_show` 5)로
정렬했다. 그래서 오전 shift 를 놓친 사람이 13:05 에 찍으면 **양쪽 모두 저녁 shift** 를
가리켰고, 지각이 조기출근(`early_clock_in_override`)으로 기록됐다.

두 경로가 **같은 함수를 부르게** 하는 것이 이 모듈의 존재 이유다. 규칙을 한쪽만
고치면 앱이 `schedule_id` 를 보내지 않는 호출에서 규칙이 다시 갈린다.

**기본 규칙 (D14)**
"시간순 첫 미출근" 하나뿐이다. 진행 중 window 우선도, 가장 가까운 미래 우선도 없다.

> ⚠️ 이 규칙은 저녁 shift 만 정상 출근하는 날에도 오전 no_show 를 먼저 제시한다.
> 오판의 방향이 뒤집힐 뿐이라, 유일한 방어선은 **판정 프리뷰 가시성**이다
> (앱이 기본 제시 카드에 "7h 12m late" 를 본문 크기로 노출해야 한다).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

# 영업일 창(operating_day → [시작, 끝) store tz aware). 호출자가 주입한다 —
# 이 모듈은 DB 를 읽지 않는 것이 존재 이유다(경계는 stores.day_start_time JSONB 에 있다).
OperatingDayWindows = Mapping[date, tuple[datetime, datetime]]

# 어제 영업일(operating_day) shift 를 후보로 남겨 두는 상한 — 종료 후 이 시간까지.
#
# 야간조는 매장 day_start 경계를 넘겨 지각하면 어제 라벨의 shift 로 찍어야 하는데,
# 후보 조회가 `operating_day == today` 뿐이면 그 shift 가 아예 보이지 않는다(D4).
# 반대로 상한이 없으면 **어제 결근한 shift 가 오늘 출근을 계속 낚아챈다** — "끝난 지
# 4시간이 넘도록 한 번도 찍지 않은 shift 로 지금 출근한다" 는 설명 가능한 상황이 아니다.
PREV_DAY_CANDIDATE_GRACE_HOURS = 4

# clock-in 불가 사유 코드 — 표시 문구는 앱(l10n)/콘솔이 소유한다. 서버는 코드만 낸다.
INELIGIBLE_ALREADY_COMPLETED = "already_completed"
INELIGIBLE_ALREADY_CLOCKED_IN = "already_clocked_in"
INELIGIBLE_CANCELLED = "cancelled"
INELIGIBLE_OUTSIDE_OPERATING_WINDOW = "outside_operating_window"
"""시작 시각이 **자기 영업일 창 밖**이다 — 고를 수는 없지만 목록에는 남는다.

영업일 D 의 창은 `[day_start(D), day_start(D+1))` 이다. 경계 04:00 매장에서
`영업일 8/19 / 시작 8/19 02:30` 같은 행이 여기 걸린다(그 시각의 영업일은 8/18 이다).
이런 행은 clock-in 대상이 될 수 없다 — 근태가 엉뚱한 shift 에 붙어 조기/지각이
하루치로 오판된다(2026-08 사고).

**그렇다고 목록에서 지우지는 않는다.** 지우면 그 스케줄이 존재한다는 사실 자체를
아무도 모른 채 사라지고, 결근인지 잘못 만든 건지 구분할 방법도 없어진다.
보이되 고를 수 없게 두고, 이유를 코드로 말한다."""


@dataclass(frozen=True)
class ShiftCandidate:
    """한 shift(=schedule) + 거기 묶인 attendance 의 사실만 담은 값.

    ORM 객체를 그대로 받지 않는 이유: 이 규칙을 단위 테스트로 고정하려면 DB 없이
    만들 수 있어야 하고, 두 호출부(identify / clock-in)가 서로 다른 쿼리 결과를
    같은 모양으로 정규화해 넣어야 하기 때문이다.
    """

    schedule_id: UUID | None
    operating_day: date | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    attendance_id: UUID | None = None
    attendance_status: str | None = None
    clock_in: datetime | None = None
    clock_out: datetime | None = None

    @property
    def is_cancelled(self) -> bool:
        return self.attendance_status == "cancelled"

    @property
    def is_done(self) -> bool:
        """이미 끝난 shift — 출근 후 퇴근까지 마쳤다."""
        return self.attendance_status == "clocked_out" or self.clock_out is not None

    @property
    def is_active(self) -> bool:
        """지금 출근 중 — 찍었고 아직 안 끝났다.

        `clock_out is None` 만으로 판단하지 않는다. status 는 clocked_out 인데
        clock_out 시각이 비어 있는 기록이 실제로 존재하며(정정 이력 등), 그걸 활성으로
        보면 이미 끝난 shift 가 목록 맨 위로 올라온다.
        """
        return (
            self.clock_in is not None and not self.is_done and not self.is_cancelled
        )

    @property
    def is_open(self) -> bool:
        """아직 한 번도 안 찍은 미출근 shift (= clock-in 후보)."""
        return self.clock_in is None and not self.is_done and not self.is_cancelled


def clock_in_eligibility(
    candidate: ShiftCandidate,
    *,
    within_window: bool = True,
) -> tuple[bool, str | None]:
    """지금 이 shift 로 clock-in 할 수 있는가 + 안 되는 이유 코드.

    **다른 shift 가 진행 중이어도 미출근 후보는 계속 eligible 이다** — 겹침 허용(D15)의
    진입로가 여기다. 겹침을 막을지 말지는 요청 단계(`allow_overlap`)에서 판단한다.
    후보 목록 단계에서 미리 막으면 "고를 수는 있는데 제출하면 거부" 보다 나쁜,
    "현장에서 대응할 방법이 화면에 없는" 상태가 된다.
    """
    if candidate.is_cancelled:
        return False, INELIGIBLE_CANCELLED
    if candidate.is_done:
        return False, INELIGIBLE_ALREADY_COMPLETED
    if candidate.clock_in is not None:
        return False, INELIGIBLE_ALREADY_CLOCKED_IN
    if not within_window:
        # 창 밖 미출근 shift — 목록에는 남기고 고르지만 못하게 한다.
        # (호출자가 창을 판정해 넘긴다. 이 모듈은 DB/설정을 읽지 않는다.)
        return False, INELIGIBLE_OUTSIDE_OPERATING_WINDOW
    return True, None


def is_open_prev_day_candidate(
    candidate: ShiftCandidate,
    *,
    now: datetime,
    grace_hours: int = PREV_DAY_CANDIDATE_GRACE_HOURS,
) -> bool:
    """어제 영업일 shift 를 오늘 후보로 남길 것인가 (D4).

    한 번도 안 찍었고, 끝난 지 `grace_hours` 를 넘기지 않았을 때만.
    """
    if not candidate.is_open:
        return False
    if candidate.scheduled_end is None:
        # 종료 시각을 모르면 상한을 걸 수 없다 — 무한히 살아남는 후보를 만들지 않는다.
        return False
    return now <= candidate.scheduled_end + timedelta(hours=grace_hours)


def is_within_operating_window(
    candidate: ShiftCandidate, *, windows: OperatingDayWindows | None
) -> bool:
    """이 shift 의 시작 시각이 자기 영업일의 창 `[day_start(D), day_start(D+1))` 안인가.

    **왜 후보 단계에서 다시 보는가** — 2026-08 오염 사고에서 `operating_day=8/18` 인데
    `start_at=8/19 17:00`(창 밖) 인 스케줄이 24건 저장됐고, 후보 필터가
    `operating_day == today` 만 봤기 때문에 그대로 clock-in 대상이 되어 "1439분 조기출근"
    으로 기록됐다. 저장 단계 검증은 이미 막지만, **이미 저장된 행**과 SQL 직접 수정·임포트
    경로는 그 검증을 지나가지 않는다. 여기가 UI 와 무관한 마지막 방어선이다.

    판단할 근거가 없으면(창 미주입, 시작 시각 없음, 그 영업일 창 미제공) **막지 않는다** —
    모르는 것을 이유로 출근을 거부하면 현장에서 복구할 방법이 없다.
    """
    if windows is None or candidate.scheduled_start is None:
        return True
    if candidate.operating_day is None:
        return True
    window = windows.get(candidate.operating_day)
    if window is None:
        return True
    start, end = window
    return start <= candidate.scheduled_start < end


def split_candidates(
    candidates: list[ShiftCandidate],
    *,
    now: datetime,
    today: date,
    grace_hours: int = PREV_DAY_CANDIDATE_GRACE_HOURS,
    windows: OperatingDayWindows | None = None,
) -> tuple[list[ShiftCandidate], list[ShiftCandidate]]:
    """(오늘 후보, 어제 후보) 로 나눈다. 취소/완료 건은 양쪽 모두에서 제외.

    어제 후보를 따로 돌려주는 이유는 **쓰임이 다르기 때문**이다:
      - 오늘 후보: 서버 fallback(자동 선택)과 walk-in 생성 판단의 대상
      - 어제 후보: **명시 `schedule_id` 선택으로만** 쓰인다
    명시 선택은 사람의 판단이지만 fallback 은 추측이고, 추측이 영업일을 하루 건너뛰면
    급여 귀속 기간이 통째로 밀린다.

    `windows` 를 주면 **자기 영업일 창 밖에서 시작하는 미출근 shift 를 제외**한다
    (`is_within_operating_window` 참조). 이미 찍은(활성) shift 는 제외하지 않는다 —
    창 밖이더라도 그 사람은 지금 근무 중이고, 목록에서 빼면 clock-out 이 불가능해진다.
    정정은 매니저의 일이지 퇴근을 막을 이유가 아니다.
    """
    today_open: list[ShiftCandidate] = []
    prev_open: list[ShiftCandidate] = []
    for candidate in candidates:
        if candidate.is_cancelled or candidate.is_done:
            continue
        if candidate.is_open and not is_within_operating_window(candidate, windows=windows):
            continue
        if candidate.operating_day == today:
            today_open.append(candidate)
        elif candidate.operating_day is not None and candidate.operating_day < today:
            if is_open_prev_day_candidate(candidate, now=now, grace_hours=grace_hours):
                prev_open.append(candidate)
    return sort_shift_candidates(today_open), sort_shift_candidates(prev_open)


def _sort_group(candidate: ShiftCandidate) -> int:
    """정렬 1군/2군/3군 (계약 §1.3)."""
    if candidate.is_active:
        return 0
    if candidate.is_open:
        return 1
    return 2


def _is_prev_day(candidate: ShiftCandidate, today: date | None) -> bool:
    """오늘 영업일보다 앞선 shift 인가 (today 를 모르면 판단하지 않는다)."""
    return (
        today is not None
        and candidate.operating_day is not None
        and candidate.operating_day < today
    )


def _sort_key(
    candidate: ShiftCandidate, today: date | None = None
) -> tuple[int, int, int, float]:
    group = _sort_group(candidate)
    if group == 0:
        # 활성 shift 끼리는 **가장 최근에 찍은 것** 이 먼저 (D15). 겹쳐서 두 개가 열려
        # 있을 때 "방금 고른 shift" 를 서버가 무상태로 되짚어 주는 유일한 단서다.
        # 활성은 어제 영업일이어도 앞이다 — 지금 해야 할 일이 clock-out 이기 때문.
        stamp = candidate.clock_in.timestamp() if candidate.clock_in else 0.0
        return (0, 0, 0, -stamp)
    # 미출근(2군) 안에서는 **오늘 후보가 언제나 어제 후보보다 앞**이다.
    # 어제 shift 의 시작 시각은 오늘 것보다 항상 이르므로, 시간순만 보면 어제
    # 결근 shift 가 무조건 0번/기본이 되어 오늘 출근을 통째로 낚아챈다
    # (급여 귀속일이 하루 밀린다). 계약 §1.6 안전규칙 2 — 어제 후보는 사람의
    # 명시 선택으로만 쓰인다 — 를 정렬에서도 지킨다.
    prev_day = 1 if (group == 1 and _is_prev_day(candidate, today)) else 0
    start = candidate.scheduled_start
    return (
        group,
        prev_day,
        0 if start is not None else 1,
        start.timestamp() if start else 0.0,
    )


def sort_shift_candidates(
    candidates: list[ShiftCandidate], *, today: date | None = None
) -> list[ShiftCandidate]:
    """계약 §1.3 정렬 — 활성 → 미출근(오늘 시간순 → 어제) → 완료/불가.

    **불변식**: 결과의 0번은 언제나 "앱이 그대로 써도 되는 대상" 이다. 구버전 HTMA 는
    `today_attendances.first` 를 무조건 자동 선택하므로(picker 가 없다), 이 불변식이
    구버전까지 원인 A 를 교정한다. clocked_out 항목은 절대 0번에 오지 않는다.

    `today` 를 주면 미출근 후보 안에서 어제 영업일 shift 를 오늘 것 뒤로 미룬다.
    오늘·어제를 섞은 목록(identify)에서는 **반드시 넘겨야 한다** — 안 넘기면 어제
    결근 shift 가 시간순 맨 앞이 되어 0번/기본을 가져간다.
    """
    return sorted(candidates, key=lambda c: _sort_key(c, today))


def pick_default_shift(
    candidates: list[ShiftCandidate], *, today: date | None = None
) -> ShiftCandidate | None:
    """앱이 기본으로 다룰 shift (계약 §1.7).

    활성 shift 가 있으면 그것(D13 — clock-out 까지 그 shift 만), 없으면 시간순 첫
    미출근(D14). 둘 다 없으면 None(워크인 경로).

    `today` 를 주면 미출근 후보 중 **오늘 것을 먼저** 고른다. 어제 후보가 기본이
    되는 것은 오늘 후보가 하나도 없을 때뿐이다(= 매장 day_start 를 넘겨 지각한
    야간조 — 그때는 그게 맞는 답이다).

    "clock-in 대상" 이 아니라 **"지금 화면의 주인공"** 이다 — 활성 shift 는
    `clock_in_eligible=false` 이면서 동시에 `is_default=true` 일 수 있다. 두 필드는
    각각 다른 질문(고를 수 있나 / 무엇을 보여주나)에 답한다.
    """
    ordered = sort_shift_candidates(candidates, today=today)
    for candidate in ordered:
        if candidate.is_active:
            return candidate
    for candidate in ordered:
        if candidate.is_open:
            return candidate
    return None


def pick_fallback_shift(
    today_candidates: list[ShiftCandidate],
) -> ShiftCandidate | None:
    """서버 fallback(= 앱이 `schedule_id` 를 안 보냈을 때) 대상 (계약 §1.8).

    **오늘 후보 중 시간순 첫 미출근** 하나뿐이다. 기존의 "진행중 window → 가장 가까운
    미래 → 가장 최근 종료 → 첫 후보" 4단 우선순위를 통째로 대체한다 — 2순위(가장 가까운
    미래)가 정확히 원인 A 였다.
    """
    for candidate in sort_shift_candidates(today_candidates):
        if candidate.is_open:
            return candidate
    return None
