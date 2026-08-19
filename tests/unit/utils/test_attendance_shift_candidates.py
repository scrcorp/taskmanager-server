"""Unit tests — app/utils/attendance_shift_candidates (clock-in 후보 규칙, 페이즈 ④).

여기서 고정하는 것은 **원인 A 를 되살릴 수 있는 규칙들**이다.

원인 A: 오전 shift 를 놓치고 그 종료시각이 지난 뒤 출근하면 서버가 *"가장 가까운
미래"* 우선순위로 **저녁 shift** 를 골랐고, 그래서 지각이 `early_clock_in_override`
로 기록됐다. 그 우선순위는 삭제됐고 규칙은 "시간순 첫 미출근" 하나뿐이다.
아래 테스트들이 그 하나를 지킨다 — 특히:

  - `pick_fallback_shift` 가 **지나간 미출근 shift** 를 미래 shift 보다 먼저 고른다
  - 정렬 0번은 언제나 "앱이 그대로 써도 되는 대상" 이다(구버전 HTMA 안전장치)
  - 어제 영업일 후보는 **fallback 이 아니라 명시 선택 전용**이다(급여 귀속일 보호)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from app.utils.attendance_shift_candidates import (
    INELIGIBLE_ALREADY_CLOCKED_IN,
    INELIGIBLE_ALREADY_COMPLETED,
    INELIGIBLE_CANCELLED,
    PREV_DAY_CANDIDATE_GRACE_HOURS,
    ShiftCandidate,
    clock_in_eligibility,
    is_open_prev_day_candidate,
    pick_default_shift,
    pick_fallback_shift,
    sort_shift_candidates,
    split_candidates,
)


TODAY = date(2026, 8, 13)
# 13:05 — 오전 shift(09-13) 가 끝난 직후. 원인 A 가 실제로 목격된 시각.
NOW = datetime(2026, 8, 13, 13, 5, tzinfo=timezone.utc)


def _at(hour: int, minute: int = 0, *, day: date = TODAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def _shift(
    *,
    start: datetime | None,
    end: datetime | None,
    operating_day: date | None = TODAY,
    status: str | None = "upcoming",
    clock_in: datetime | None = None,
    clock_out: datetime | None = None,
) -> ShiftCandidate:
    return ShiftCandidate(
        schedule_id=uuid4(),
        operating_day=operating_day,
        scheduled_start=start,
        scheduled_end=end,
        attendance_id=uuid4(),
        attendance_status=status,
        clock_in=clock_in,
        clock_out=clock_out,
    )


# ---------------------------------------------------------------------------
# 상태 술어 — is_open / is_active / is_done
# ---------------------------------------------------------------------------


def test_open_means_never_clocked_in() -> None:
    """미출근 = 아직 한 번도 안 찍음. 종료시각이 지났어도 여전히 후보다.

    지나간 미출근 shift 를 후보에서 빼면 C안(선택 화면)을 해도 원인 A 가 그대로
    남는다 — 지각을 제자리에 기록할 대상 자체가 사라지기 때문이다.
    """
    passed = _shift(start=_at(9), end=_at(13), status="no_show")
    assert passed.is_open is True
    assert passed.is_active is False
    assert passed.is_done is False


def test_active_requires_clock_in_and_not_done() -> None:
    working = _shift(start=_at(9), end=_at(13), status="working", clock_in=_at(9, 2))
    assert working.is_active is True
    assert working.is_open is False


def test_clocked_out_status_without_clock_out_time_is_not_active() -> None:
    """status 는 clocked_out 인데 clock_out 시각이 빈 기록(정정 이력 등).

    `clock_out is None` 만 보고 활성으로 취급하면 **이미 끝난 shift 가 목록 맨 위**로
    올라와 앱의 기본 제시가 통째로 틀어진다.
    """
    weird = _shift(
        start=_at(9), end=_at(13), status="clocked_out", clock_in=_at(9), clock_out=None
    )
    assert weird.is_done is True
    assert weird.is_active is False
    assert weird.is_open is False


def test_cancelled_is_neither_open_nor_active() -> None:
    cancelled = _shift(start=_at(9), end=_at(13), status="cancelled")
    assert cancelled.is_open is False
    assert cancelled.is_active is False


# ---------------------------------------------------------------------------
# clock_in_eligibility
# ---------------------------------------------------------------------------


def test_eligibility_open_shift_is_selectable() -> None:
    eligible, reason = clock_in_eligibility(_shift(start=_at(17), end=_at(21)))
    assert eligible is True
    assert reason is None


def test_eligibility_reason_codes() -> None:
    """불가 사유는 **코드**로만 낸다 — 표시 문구는 앱(l10n)/콘솔이 소유한다."""
    done = _shift(
        start=_at(9), end=_at(13), status="clocked_out",
        clock_in=_at(9), clock_out=_at(13),
    )
    assert clock_in_eligibility(done) == (False, INELIGIBLE_ALREADY_COMPLETED)

    working = _shift(start=_at(9), end=_at(13), status="working", clock_in=_at(9))
    assert clock_in_eligibility(working) == (False, INELIGIBLE_ALREADY_CLOCKED_IN)

    cancelled = _shift(start=_at(9), end=_at(13), status="cancelled")
    assert clock_in_eligibility(cancelled) == (False, INELIGIBLE_CANCELLED)


def test_other_shift_being_active_does_not_block_eligibility() -> None:
    """다른 shift 가 진행 중이어도 미출근 후보는 계속 eligible.

    겹침 허용(D15)의 진입로가 여기다. 후보 단계에서 미리 막으면 "현장에서 대응할
    방법이 화면에 아예 없는" 상태가 되고, 막을지 말지는 요청 단계(`allow_overlap`)
    에서 판단한다.
    """
    evening = _shift(start=_at(17), end=_at(21))
    assert clock_in_eligibility(evening)[0] is True


# ---------------------------------------------------------------------------
# 정렬 (계약 §1.3)
# ---------------------------------------------------------------------------


def test_sort_puts_open_morning_before_upcoming_evening() -> None:
    """원인 A 회귀 — 미출근 오전(no_show) 이 저녁 upcoming 보다 앞이어야 한다.

    예전 정렬은 status 랭크(`upcoming` 4 < `no_show` 5)라 저녁이 primary 였고,
    앱이 그 schedule_id 를 실어 보내 지각이 조기출근으로 기록됐다.
    """
    morning = _shift(start=_at(9), end=_at(13), status="no_show")
    evening = _shift(start=_at(17), end=_at(21), status="upcoming")

    assert sort_shift_candidates([evening, morning]) == [morning, evening]


def test_sort_active_shift_wins_over_every_open_shift() -> None:
    """D13 — 한 번 clock-in 하면 clock-out 까지 그 shift 가 화면의 주인공이다."""
    morning = _shift(start=_at(9), end=_at(13), status="no_show")
    evening = _shift(
        start=_at(17), end=_at(21), status="working", clock_in=_at(12, 55)
    )

    assert sort_shift_candidates([morning, evening])[0] is evening


def test_sort_active_shifts_use_most_recent_clock_in_first() -> None:
    """겹쳐서 둘 다 열려 있을 때 D15 "가장 최근에 선택한 shift" 를 서버 무상태로.

    앱이 마지막 선택을 로컬에 기억하면 기기를 바꾸거나 재시작하는 순간 규칙이 사라진다.
    """
    first = _shift(start=_at(9), end=_at(13), status="working", clock_in=_at(9, 1))
    second = _shift(start=_at(17), end=_at(21), status="working", clock_in=_at(13, 5))

    assert sort_shift_candidates([first, second]) == [second, first]


def test_sort_never_puts_a_finished_shift_first() -> None:
    """구버전 HTMA 안전장치 — 구버전은 `today_attendances.first` 를 무조건 자동 선택한다.

    0번이 항상 "그대로 써도 되는 대상" 이라는 불변식 덕분에 **구버전도 배포 즉시**
    원인 A 가 교정된다. 이 불변식이 깨지면 구버전이 끝난 shift 로 출근을 시도한다.
    """
    done = _shift(
        start=_at(9), end=_at(13), status="clocked_out",
        clock_in=_at(9), clock_out=_at(13),
    )
    evening = _shift(start=_at(17), end=_at(21))

    ordered = sort_shift_candidates([done, evening])
    assert ordered[0] is evening
    assert ordered[-1] is done


def test_sort_places_unknown_start_after_known_ones() -> None:
    """시작 시각을 모르는 후보는 시간순에서 뒤로 — 기본 제시로 뽑히지 않게."""
    known = _shift(start=_at(17), end=_at(21))
    unknown = _shift(start=None, end=None)

    assert sort_shift_candidates([unknown, known]) == [known, unknown]


# ---------------------------------------------------------------------------
# pick_default_shift (계약 §1.7)
# ---------------------------------------------------------------------------


def test_default_is_the_active_shift_even_though_it_is_not_selectable() -> None:
    """`is_default` 와 `clock_in_eligible` 은 **서로 다른 질문**에 답한다.

    "지금 화면의 주인공인가" 와 "고를 수 있나" 는 별개다 — 진행 중 shift 는 기본이면서
    동시에 clock-in 불가다.
    """
    working = _shift(start=_at(9), end=_at(13), status="working", clock_in=_at(9))
    evening = _shift(start=_at(17), end=_at(21))

    default = pick_default_shift([evening, working])
    assert default is working
    assert clock_in_eligibility(default)[0] is False


def test_default_is_first_open_when_nothing_is_active() -> None:
    morning = _shift(start=_at(9), end=_at(13), status="no_show")
    evening = _shift(start=_at(17), end=_at(21))

    assert pick_default_shift([evening, morning]) is morning


def test_default_is_none_when_everything_is_done() -> None:
    """워크인 경로 — 앱은 null 을 보고 "고를 shift 가 없다" 로 읽는다."""
    done = _shift(
        start=_at(9), end=_at(13), status="clocked_out",
        clock_in=_at(9), clock_out=_at(13),
    )
    assert pick_default_shift([done]) is None
    assert pick_default_shift([]) is None


# ---------------------------------------------------------------------------
# pick_fallback_shift (계약 §1.8) — 원인 A 의 근본 수정
# ---------------------------------------------------------------------------


def test_fallback_picks_the_missed_morning_shift_not_the_evening_one() -> None:
    """★ 이 트랙의 핵심 회귀 테스트 (13:05 출근).

    예전 우선순위 2순위가 "가장 가까운 미래" 였기 때문에 17:00 shift 가 잡혔다.
    이제는 시간순 첫 미출근 = 09:00 shift 다.
    """
    morning = _shift(start=_at(9), end=_at(13), status="no_show")
    evening = _shift(start=_at(17), end=_at(21), status="upcoming")

    assert pick_fallback_shift([evening, morning]) is morning


def test_fallback_skips_active_shift_and_takes_next_open_one() -> None:
    """fallback 대상은 **미출근**뿐이다 — 진행 중 shift 에 두 번 찍히지 않는다."""
    working = _shift(start=_at(9), end=_at(13), status="working", clock_in=_at(9))
    evening = _shift(start=_at(17), end=_at(21))

    assert pick_fallback_shift([working, evening]) is evening


def test_fallback_is_none_when_only_active_shifts_remain() -> None:
    working = _shift(start=_at(9), end=_at(13), status="working", clock_in=_at(9))
    assert pick_fallback_shift([working]) is None


# ---------------------------------------------------------------------------
# split_candidates — D4 야간조 + 급여 귀속일 보호
# ---------------------------------------------------------------------------


def test_split_keeps_yesterday_out_of_the_today_bucket() -> None:
    """어제 후보를 오늘 바구니에 섞으면 fallback 이 영업일을 하루 건너뛴다.

    fallback 은 추측이고, 추측이 영업일을 옮기면 급여 귀속 기간이 통째로 밀린다.
    어제 후보는 **명시 선택 전용**이라 따로 돌려준다.
    """
    yesterday = TODAY - timedelta(days=1)
    night = _shift(
        start=_at(21, day=yesterday),
        end=_at(1, day=TODAY),
        operating_day=yesterday,
    )
    today_shift = _shift(start=_at(17), end=_at(21))

    # 새벽 04:30 — 야간조가 매장 day_start 경계를 넘겨 지각한 그 시각.
    now = _at(4, 30)
    today_open, prev_open = split_candidates(
        [night, today_shift], now=now, today=TODAY
    )
    assert today_open == [today_shift]
    assert prev_open == [night]


def test_split_drops_yesterday_shift_past_the_grace_window() -> None:
    """상한이 없으면 어제 결근한 shift 가 오늘 출근을 계속 낚아챈다.

    "끝난 지 4시간이 넘도록 한 번도 찍지 않은 shift 로 지금 출근한다" 는 설명 가능한
    상황이 아니다.
    """
    yesterday = TODAY - timedelta(days=1)
    stale = _shift(
        start=_at(9, day=yesterday),
        end=_at(13, day=yesterday),
        operating_day=yesterday,
    )
    _today_open, prev_open = split_candidates([stale], now=NOW, today=TODAY)
    assert prev_open == []


def test_grace_window_boundary_is_inclusive() -> None:
    yesterday = TODAY - timedelta(days=1)
    end = _at(9, day=yesterday)
    shift = _shift(start=_at(5, day=yesterday), end=end, operating_day=yesterday)

    on_edge = end + timedelta(hours=PREV_DAY_CANDIDATE_GRACE_HOURS)
    assert is_open_prev_day_candidate(shift, now=on_edge) is True
    assert (
        is_open_prev_day_candidate(shift, now=on_edge + timedelta(minutes=1)) is False
    )


def test_yesterday_candidate_without_end_time_is_dropped() -> None:
    """종료 시각을 모르면 상한을 걸 수 없다 — 무한히 살아남는 후보를 만들지 않는다."""
    yesterday = TODAY - timedelta(days=1)
    shift = _shift(start=_at(21, day=yesterday), end=None, operating_day=yesterday)
    assert is_open_prev_day_candidate(shift, now=NOW) is False


def test_split_drops_finished_and_cancelled_from_both_buckets() -> None:
    """어제 후보에도 clocked_out 제외가 걸린다 — 끝난 야간 shift 가 되살아나지 않게."""
    yesterday = TODAY - timedelta(days=1)
    night_done = _shift(
        start=_at(21, day=yesterday),
        end=_at(1, day=TODAY),
        operating_day=yesterday,
        status="clocked_out",
        clock_in=_at(21, day=yesterday),
        clock_out=_at(1, day=TODAY),
    )
    today_cancelled = _shift(start=_at(17), end=_at(21), status="cancelled")

    today_open, prev_open = split_candidates(
        [night_done, today_cancelled], now=NOW, today=TODAY
    )
    assert today_open == []
    assert prev_open == []


def test_split_keeps_yesterday_active_shift_out_of_candidates() -> None:
    """어제 shift 에 이미 찍고 안 닫은 건은 **후보가 아니다**(clock-in 대상이 아님).

    그 row 로 break/clock-out 을 하는 경로는 따로 있다(`_active_row`).
    """
    yesterday = TODAY - timedelta(days=1)
    night_open = _shift(
        start=_at(21, day=yesterday),
        end=_at(1, day=TODAY),
        operating_day=yesterday,
        status="working",
        clock_in=_at(21, day=yesterday),
    )
    _today_open, prev_open = split_candidates([night_open], now=NOW, today=TODAY)
    assert prev_open == []


# ---------------------------------------------------------------------------
# 오늘·어제를 **섞은** 목록 — identify 가 쓰는 모양 (계약 §1.6 안전규칙 2)
# ---------------------------------------------------------------------------


def test_sort_demotes_yesterday_open_shift_below_todays() -> None:
    """★ 어제 결근 shift 가 오늘 출근을 낚아채지 못한다.

    2군(미출근) 정렬 키가 `scheduled_start ASC` 뿐이면 어제 shift 는 **항상** 오늘
    것보다 이르므로 무조건 0번이 된다. 구버전 HTMA 는 `today_attendances.first` 를
    그대로 실어 보내고 서버는 그것을 '명시 선택' 으로 수용하므로, 오늘 근무가
    통째로 어제 영업일(= 다른 급여 기간일 수 있다)에 귀속된다.
    """
    yesterday = TODAY - timedelta(days=1)
    night = _shift(
        start=_at(22, day=yesterday),
        end=_at(6, day=TODAY),
        operating_day=yesterday,
        status="no_show",
    )
    morning = _shift(start=_at(8), end=_at(16))

    assert sort_shift_candidates([night, morning], today=TODAY) == [morning, night]
    assert pick_default_shift([night, morning], today=TODAY) is morning


def test_yesterday_open_shift_is_still_the_default_when_today_has_none() -> None:
    """오늘 후보가 하나도 없으면 어제 후보가 기본이다 — D4 야간조가 그 경우다.

    매장 day_start 를 넘겨 지각한 야간조에게는 그게 유일하게 맞는 답이다.
    (fallback = `schedule_id` 미전송 경로는 이때도 어제로 넘어가지 않는다 —
     `pick_fallback_shift` 는 오늘 후보만 본다.)
    """
    yesterday = TODAY - timedelta(days=1)
    night = _shift(
        start=_at(22, day=yesterday),
        end=_at(6, day=TODAY),
        operating_day=yesterday,
    )
    assert pick_default_shift([night], today=TODAY) is night


def test_yesterday_active_shift_outranks_todays_open_shift() -> None:
    """어제 야간조가 **진행 중**이면 그게 화면의 주인공이다 (D13).

    지금 해야 할 일은 clock-out 이다 — 오늘 shift 를 앞세우면 앱이 Clock Out 대신
    Clock In 을 제시하고, 서버의 열린-row 가드가 그걸 400 으로 막는다.
    """
    yesterday = TODAY - timedelta(days=1)
    night = _shift(
        start=_at(22, day=yesterday),
        end=_at(6, day=TODAY),
        operating_day=yesterday,
        status="working",
        clock_in=_at(22, day=yesterday),
    )
    morning = _shift(start=_at(8), end=_at(16))

    assert sort_shift_candidates([morning, night], today=TODAY)[0] is night
    assert pick_default_shift([morning, night], today=TODAY) is night


# ---------------------------------------------------------------------------
# 영업일 창 밖 시프트 배제 (2026-08 시작일 오프셋 오염 사고)
# ---------------------------------------------------------------------------
#
# 사고 재현: 경계 11:00 매장에서 `operating_day=8/18` 인데 `start_at=8/19 17:00`
# (= 8/19 영업일의 창) 인 스케줄이 24건 저장됐다. 후보 필터가 `operating_day == today`
# 만 봤기 때문에 8/18 에 출근하면 그 시프트가 잡혔고 "1439분 조기출근" 으로 기록됐다.
# 저장 단계 검증이 지금은 막지만, **이미 저장된 행**과 SQL 직접 수정·임포트 경로는
# 그 검증을 지나가지 않는다. 아래가 UI 와 무관한 마지막 방어선이다.

# 경계 11:00 매장 — 영업일 D 의 창은 [D 11:00, D+1 11:00).
def _window(day: date) -> tuple[datetime, datetime]:
    return _at(11, day=day), _at(11, day=day + timedelta(days=1))


def _windows(*days: date) -> dict[date, tuple[datetime, datetime]]:
    return {d: _window(d) for d in days}


def test_shift_starting_outside_its_operating_day_window_is_dropped() -> None:
    """`operating_day=오늘` 라벨을 달고 **내일 창**에서 시작하는 시프트는 후보가 아니다."""
    corrupted = _shift(
        start=_at(17, day=TODAY + timedelta(days=1)),   # 창 밖(내일 17:00)
        end=_at(22, day=TODAY + timedelta(days=1)),
    )
    healthy = _shift(start=_at(17), end=_at(22))

    today_open, _prev = split_candidates(
        [corrupted, healthy], now=NOW, today=TODAY, windows=_windows(TODAY)
    )
    assert today_open == [healthy]


def test_dawn_shift_inside_the_window_is_kept() -> None:
    """경계 이전 새벽 시각은 **달력상 D+1** 이며 창 안이다 — 이건 정상 시프트다."""
    dawn = _shift(
        start=_at(3, day=TODAY + timedelta(days=1)),    # 03:00 < 경계 11:00 → 창 안
        end=_at(9, day=TODAY + timedelta(days=1)),
    )
    today_open, _prev = split_candidates(
        [dawn], now=_at(2, 30, day=TODAY + timedelta(days=1)),
        today=TODAY, windows=_windows(TODAY),
    )
    assert today_open == [dawn]


def test_window_start_is_inclusive_and_end_is_exclusive() -> None:
    """경계 시각 정각 시작은 창 안, 다음 영업일 경계 정각은 창 밖(= 다음 영업일 소속)."""
    on_boundary = _shift(start=_at(11), end=_at(19))
    next_boundary = _shift(
        start=_at(11, day=TODAY + timedelta(days=1)),
        end=_at(19, day=TODAY + timedelta(days=1)),
    )
    today_open, _prev = split_candidates(
        [on_boundary, next_boundary], now=NOW, today=TODAY, windows=_windows(TODAY)
    )
    assert today_open == [on_boundary]


def test_yesterday_shift_is_checked_against_its_own_window() -> None:
    """어제 후보의 창은 **어제 것**이다 — 오늘 창으로 재면 정상 야간조가 사라진다."""
    yesterday = TODAY - timedelta(days=1)
    night = _shift(
        start=_at(21, day=yesterday),
        end=_at(1, day=TODAY),
        operating_day=yesterday,
    )
    _today_open, prev_open = split_candidates(
        [night], now=_at(4, 30), today=TODAY, windows=_windows(TODAY, yesterday)
    )
    assert prev_open == [night]


def test_active_shift_is_kept_even_when_outside_the_window() -> None:
    """이미 찍은 시프트는 창 밖이어도 남긴다 — 빼면 clock-out 이 불가능해진다.

    날짜 오염의 정정은 매니저의 일이지, 근무 중인 사람의 퇴근을 막을 이유가 아니다.
    """
    corrupted_active = _shift(
        start=_at(17, day=TODAY + timedelta(days=1)),
        end=_at(22, day=TODAY + timedelta(days=1)),
        status="working",
        clock_in=_at(12),
    )
    today_open, _prev = split_candidates(
        [corrupted_active], now=NOW, today=TODAY, windows=_windows(TODAY)
    )
    assert today_open == [corrupted_active]


def test_without_windows_nothing_is_dropped() -> None:
    """창을 모르면 막지 않는다 — 모른다는 이유로 출근을 거부하면 현장에 복구 수단이 없다."""
    corrupted = _shift(
        start=_at(17, day=TODAY + timedelta(days=1)),
        end=_at(22, day=TODAY + timedelta(days=1)),
    )
    today_open, _prev = split_candidates([corrupted], now=NOW, today=TODAY)
    assert today_open == [corrupted]


def test_shift_without_start_time_is_not_dropped_by_window() -> None:
    """시작 시각이 없으면 판단 근거가 없다 — 창 검사로 떨어뜨리지 않는다."""
    no_start = _shift(start=None, end=_at(19))
    today_open, _prev = split_candidates(
        [no_start], now=NOW, today=TODAY, windows=_windows(TODAY)
    )
    assert today_open == [no_start]
