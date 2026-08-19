"""타임존 유틸리티 — 매장/조직 타임존 해석 헬퍼.

Timezone utility — helpers for resolving store/organization timezone.
Includes day boundary logic for determining work_date.
"""

from datetime import date, datetime, time, timedelta as _timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization, Store

DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_DAY_START_TIME = "06:00"

# Weekday name mapping (Python weekday() -> JSONB key)
_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def floor_to_minute(value: datetime | None) -> datetime | None:
    """초·마이크로초를 버려 분 경계로 절삭합니다.

    Truncate a datetime to the minute (drop seconds/microseconds).

    기록(DB)은 초까지 보존하되 표시·계산은 분 단위로만 한다는 규칙(R1/R2)의 기반.
    표시가 `HH:MM` (초 버림) 이므로 계산도 같은 절삭을 거쳐야 화면 시각끼리의
    뺄셈과 지표가 항상 일치한다.
    """
    if value is None:
        return None
    return value.replace(second=0, microsecond=0)


def minutes_between(start: datetime | None, end: datetime | None) -> int:
    """두 시각 사이의 분 — 각각 분 절삭한 뒤 차이 (R2).

    Minutes between two datetimes, each truncated to the minute first.

    `int((end - start).total_seconds() / 60)` (차이를 내림) 과 다르다:
      22:26:50 → 22:57:20 은 차이 내림이면 30분이지만,
      화면에는 `22:26 – 22:57` 로 보이므로 31분이어야 맞다.

    start/end 중 하나라도 None 이면 0. 음수는 0 으로 하한.
    """
    s = floor_to_minute(start)
    e = floor_to_minute(end)
    if s is None or e is None:
        return 0
    return max(0, int((e - s).total_seconds() // 60))


async def get_store_timezone(db: AsyncSession, store_id: UUID) -> str:
    """매장의 유효 타임존을 반환합니다 (매장 → 조직 → 기본값 순).

    Resolve effective timezone for a store (store → organization → default).

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        store_id: 매장 UUID (Store UUID)

    Returns:
        str: IANA 타임존 문자열 (IANA timezone string)
    """
    result = await db.execute(
        select(Store.timezone, Organization.timezone.label("org_timezone"))
        .join(Organization, Store.organization_id == Organization.id)
        .where(Store.id == store_id)
    )
    row = result.one_or_none()
    if row is None:
        return DEFAULT_TIMEZONE
    return row.timezone or row.org_timezone or DEFAULT_TIMEZONE


async def get_org_timezone(db: AsyncSession, organization_id: UUID) -> str:
    """조직의 타임존을 반환합니다.

    Get the organization's timezone.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        organization_id: 조직 UUID (Organization UUID)

    Returns:
        str: IANA 타임존 문자열 (IANA timezone string)
    """
    result = await db.execute(
        select(Organization.timezone).where(Organization.id == organization_id)
    )
    tz = result.scalar_one_or_none()
    return tz or DEFAULT_TIMEZONE


def resolve_timezone(client_timezone: str | None, store_timezone: str) -> str:
    """클라이언트 타임존과 매장 타임존 중 유효한 값을 반환합니다.

    Resolve effective timezone: client override → store/org default.

    Args:
        client_timezone: 클라이언트가 전송한 타임존 (Client-sent timezone, may be None)
        store_timezone: 매장/조직 타임존 (Store/org timezone)

    Returns:
        str: 유효한 IANA 타임존 (Effective IANA timezone)
    """
    return client_timezone or store_timezone


def resolve_day_start_time(day_start_time: dict | None, weekday: int) -> time:
    """매장의 day_start_time JSONB에서 해당 요일의 경계 시각을 반환합니다.

    Resolve the day boundary start time for a given weekday from store config.

    Args:
        day_start_time: JSONB config — {"all": "06:00"} or {"mon": "06:00", ...}
        weekday: Python weekday (0=Mon, 6=Sun)

    Returns:
        time: 해당 요일의 경계 시각 (Day boundary time for the weekday)
    """
    if not day_start_time:
        h, m = DEFAULT_DAY_START_TIME.split(":")
        return time(int(h), int(m))

    day_key = _WEEKDAY_KEYS[weekday]
    time_str = day_start_time.get(day_key) or day_start_time.get("all") or DEFAULT_DAY_START_TIME
    h, m = time_str.split(":")
    return time(int(h), int(m))


def day_start_map(day_start_time: dict | None) -> dict[str, str]:
    """요일별 경계 시각을 **7키 전부 채운** 매핑으로 펼칩니다 (클라이언트 전달용).

    Expand the store day-boundary config into a complete mon..sun map.

    저장 형태(`{"all": "11:00"}` 또는 일부 요일만)를 그대로 내려주면 클라이언트가
    "없는 요일 → all → 서버 기본값" 폴백 사슬을 다시 구현해야 하고, 그 사슬이 서버와
    한 칸이라도 어긋나면 화면의 날짜와 저장되는 날짜가 갈린다. 펼쳐서 내려주면
    클라이언트에 남는 일은 "그 요일 키를 읽는다" 뿐이다.
    """
    return {
        key: resolve_day_start_time(day_start_time, index).strftime("%H:%M")
        for index, key in enumerate(_WEEKDAY_KEYS)
    }


def operating_day_of(day_start_time: dict | None, moment: datetime) -> date:
    """벽시계 시각이 속한 **영업일 라벨**. `operating_day_window` 의 역함수다.

    Which operating day a wall-clock moment belongs to.

    시작 달력일 파생 규칙 `so = 시작시각 < day_start(D+1) ? 1 : 0` 을 뒤집은 것이다 —
    경계 이전 시각은 전날 영업일에 속한다. (`get_work_date` 와 같은 규칙이지만 이쪽은
    이미 매장 tz 로 표현된 벽시계 datetime 을 받는다.)
    """
    boundary = resolve_day_start_time(day_start_time, moment.date().weekday())
    if moment.time() < boundary:
        return moment.date() - _timedelta(days=1)
    return moment.date()


def operating_day_window(
    tz_name: str, day_start_time: dict | None, operating_day: date
) -> tuple[datetime, datetime]:
    """영업일 D 의 창 `[D의 경계, D+1의 경계)` — store tz aware (끝은 배타).

    영업일 라벨과 달력일이 다를 수 있는 매장(경계가 자정이 아닌 곳)에서 "이 시각이
    정말 그 영업일에 속하는가" 를 묻는 **단일 기준**이다. 시작 달력일 파생 규칙
    (`so = 시작시각 < day_start(D+1) ? 1 : 0`) 과 같은 경계를 쓴다 — 두 곳이 갈리면
    저장은 통과하는데 근태는 그 시프트를 못 찾는 상태가 만들어진다.
    """
    tz = ZoneInfo(tz_name)
    next_day = operating_day + _timedelta(days=1)
    start = datetime.combine(
        operating_day, resolve_day_start_time(day_start_time, operating_day.weekday()), tzinfo=tz
    )
    end = datetime.combine(
        next_day, resolve_day_start_time(day_start_time, next_day.weekday()), tzinfo=tz
    )
    return start, end


def store_day_start_from_org(org_day_start: time | None) -> dict | None:
    """조직 기본 경계(Time 단일값) → 매장 day_start_time JSONB 형태로 감쌉니다 (D2-2).

    org 는 `Time` 하나, store 는 요일별 JSONB 라 모양이 다르다. 이 변환이
    호출부마다 흩어지면 `{"all": ...}` 를 빠뜨린 쪽이 조용히 기본 06:00 으로
    떨어진다 — 그래서 변환은 여기 한 곳에만 둔다.

    쓰임은 **매장 생성 시 스냅샷 복사** 하나뿐이다. 라이브 cascade 가 아니다:
    조직 기본값을 나중에 바꿨을 때 기존 매장의 경계가 따라 움직이면 이미 확정된
    과거 집계(급여·리포트)가 소리 없이 흔들린다(D1 "사건은 재해석하지 않는다").

    Args:
        org_day_start: organizations.day_start_time (Time, 미설정이면 None)

    Returns:
        dict | None: {"all": "HH:MM"} 또는 org 미설정 시 None
                     (None 이면 매장도 미설정 → DEFAULT_DAY_START_TIME 폴백)
    """
    if org_day_start is None:
        return None
    return {"all": f"{org_day_start.hour:02d}:{org_day_start.minute:02d}"}


# ─── 일자별 시간 범위 설정 값 (D2-7 / D2-8) ────────────────────────
#
# `store.operating_hours`(영업시간)와 `schedule.range`(직원 근무 가능 시간대)가
# **공유하는 값 형태**. 두 키의 의미는 다르지만 모양이 하나여야 파서도 하나로 끝난다.
#
#     {"mode": "all" | "per_day",
#      "all":     {"start": "11:00", "end": "02:00", "end_offset_days": 1},
#      "per_day": {"mon": {...}, ...},
#      "closed":  ["mon"]}
#
# 시각은 언제나 `00:00~23:59`. 자정 넘김은 `end_offset_days` 로만 표현한다 —
# `26:00` 같은 24 초과 표기와 "종료 < 시작이면 다음 날" 암묵 보정은 **둘 다 폐기**됐다(D2-8).
# 암묵 보정이 이번 사고들의 원인이었다: 앱은 규칙을 몰라 23:59 로 clamp 했고,
# 서버는 알고 있었고, 부분 수정에서는 적용되다 말았다.
#
# 24시간 운영의 모호성도 오프셋이 해소한다 —
# `11:00–11:00 offset 0` = 0시간, `11:00–11:00 offset 1` = 24시간.

MINUTES_PER_DAY = 1440


def parse_wall_clock_minutes(value: str | None) -> int | None:
    """`"HH:MM"`(00:00~23:59) → 자정 기준 분. 형식·범위를 벗어나면 None.

    **24 이상을 받지 않는다.** 옛 `26:00` 저장값은 마이그레이션이 오프셋 형태로
    정규화하며, 여기서 관용적으로 받아주면 폐기한 표기가 되살아난다.
    파싱 실패는 예외가 아니라 None — 설정 하나가 리포트 전체를 죽이지 않게.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        hh, mm = value.split(":", 1)
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def _range_entry_to_minutes(entry: dict | None) -> tuple[int, int] | None:
    """`{"start","end","end_offset_days"}` 한 칸 → (시작분, 종료분).

    종료분은 **시작과 같은 날 자정 기준**이라 오프셋만큼 1440 이 더해진다.
    따라서 `end >= start` 가 자동으로 성립하고, 길이 0(= start==end, offset 0)도
    그대로 표현된다.
    """
    if not isinstance(entry, dict):
        return None
    start = parse_wall_clock_minutes(entry.get("start"))
    end = parse_wall_clock_minutes(entry.get("end"))
    if start is None or end is None:
        return None
    offset = entry.get("end_offset_days", 0)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        offset = 0
    end += offset * MINUTES_PER_DAY
    if end < start:
        # 오프셋이 잘못 들어와 음수 길이가 된 경우. 암묵 보정(+1일)을 하지 않는다 —
        # 보정하면 잘못된 설정이 그럴듯하게 동작해 아무도 고치지 않는다.
        return None
    return (start, end)


def is_closed_weekday(value: dict | None, weekday: int) -> bool:
    """그 요일이 휴무로 지정됐는지 (D2-6).

    **휴무는 `closed` 배열 하나로만 표현한다.** `per_day` 에서 요일 키를 지우는 것은
    휴무가 아니라 `all` 폴백이다 — 둘을 섞으면 "월요일 설정을 지웠더니 매장이
    문을 닫은 것으로 집계"되는 사고가 난다.
    """
    if not isinstance(value, dict):
        return False
    closed = value.get("closed")
    if not isinstance(closed, list):
        return False
    return _WEEKDAY_KEYS[weekday] in closed


def resolve_day_range(value: dict | None, weekday: int) -> tuple[int, int] | None:
    """설정 값에서 해당 요일의 (시작분, 종료분)을 뽑는다. 표준 형태 전용 파서.

    반환 None 의 의미는 셋 다 "적용할 범위가 없다"로 같다 —
    미설정 / 휴무 / 값이 표준 형태가 아님. 호출부는 셋을 구분할 필요가 없고,
    구분이 필요하면 `is_closed_weekday()` 를 따로 부른다.

    Args:
        value: `{mode, all, per_day, closed}` 표준 값
        weekday: Python weekday (0=Mon, 6=Sun)

    Returns:
        tuple[int, int] | None: (시작분, 종료분). 종료분은 오프셋이 반영돼 1440 을 넘을 수 있다.
    """
    if not isinstance(value, dict):
        return None
    if is_closed_weekday(value, weekday):
        return None

    if value.get("mode") == "per_day":
        per_day = value.get("per_day")
        if isinstance(per_day, dict):
            got = _range_entry_to_minutes(per_day.get(_WEEKDAY_KEYS[weekday]))
            if got is not None:
                return got
            # 그 요일 키가 없으면 all 폴백. 휴무가 아니다 (위 is_closed_weekday 참조).

    return _range_entry_to_minutes(value.get("all"))


def get_work_date(tz_name: str, day_start_time: dict | None, utc_now: datetime | None = None) -> date:
    """경계 시각 기준으로 work_date를 결정합니다.

    Determine work_date based on the store's day boundary time.
    If current local time < day_start_time, work_date = yesterday.

    Example: day_start_time = 06:00
      - 2026-04-01 03:00 local → work_date = 2026-03-31
      - 2026-04-01 07:00 local → work_date = 2026-04-01

    Args:
        tz_name: IANA timezone string (e.g. "America/Los_Angeles")
        day_start_time: JSONB config — {"all": "06:00"} or per-day
        utc_now: UTC timestamp (defaults to current time)

    Returns:
        date: 경계 시각 기준 work_date (Work date based on boundary)
    """
    if utc_now is None:
        utc_now = datetime.now(ZoneInfo("UTC"))

    tz = ZoneInfo(tz_name)
    local_now = utc_now.astimezone(tz)
    local_date = local_now.date()
    local_time = local_now.time()

    boundary = resolve_day_start_time(day_start_time, local_date.weekday())

    if local_time < boundary:
        from datetime import timedelta
        return local_date - timedelta(days=1)
    return local_date


async def get_store_day_config(db: AsyncSession, store_id: UUID) -> tuple[str, dict | None]:
    """매장의 타임존과 day_start_time을 한 번의 쿼리로 조회합니다.

    Fetch store timezone and day_start_time in a single query.

    Args:
        db: Async database session
        store_id: Store UUID

    Returns:
        tuple[str, dict | None]: (effective_timezone, day_start_time JSONB)
    """
    result = await db.execute(
        select(
            Store.timezone,
            Store.day_start_time,
            Organization.timezone.label("org_timezone"),
        )
        .join(Organization, Store.organization_id == Organization.id)
        .where(Store.id == store_id)
    )
    row = result.one_or_none()
    if row is None:
        return DEFAULT_TIMEZONE, None
    tz = row.timezone or row.org_timezone or DEFAULT_TIMEZONE
    return tz, row.day_start_time


def interpret_clock_time(value: datetime, tz_name: str) -> datetime:
    """수동 입력 clock 시각을 timezone-aware UTC instant 로 정규화합니다.

    Normalize a manually-entered clock time to an aware UTC instant.

    - naive 값 → 매장 타임존(tz_name)의 벽시계로 해석 (디바이스/브라우저/서버
      로컬 타임존을 절대 신뢰하지 않는다).
    - offset 명시 값 → 그 offset 을 존중해 UTC 로 변환만 수행.

    clock_in/clock_out 저장 계약(aware UTC instant)을 한 곳에서 강제 —
    naive 시각이 UTC 나 서버 로컬 시간으로 오기록되어 instant 가 시간 단위로
    밀리던 payroll 버그(AK-1) 방지.

    Args:
        value: 수동 입력 시각 (naive 또는 aware)
        tz_name: 매장 유효 타임존 (IANA, get_store_timezone cascade 결과)

    Returns:
        datetime: timezone-aware UTC instant
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(tz_name))
    return value.astimezone(ZoneInfo("UTC"))


def assemble_shift_datetimes(
    operating_day: date,
    start_time: time | None,
    end_time: time | None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> tuple[datetime | None, datetime | None]:
    """[TRANSITION-ONLY] 시각(+선택적 명시 날짜)을 naive datetime으로 조립합니다.

    ⚠️ 전환용 헬퍼 — 목표 상태에선 클라가 start_at/end_at(ISO)을 직접 보내므로 불필요.
    쓰임은 둘뿐: ① 마이그레이션 백필(현재 SQL로 처리), ② Wave 1 구(舊) start_time 요청 shim.
    Wave 3에서 제거. 영구 계산 헬퍼는 net_minutes_from_datetimes(순수 datetime).

    - start_date 미지정 시 operating_day(영업일 라벨)를 앵커로 사용.
    - end_date 미지정 시 start_date를 쓰되, end_time ≤ start_time이면 +1일(자정 넘김).
    저장/해석은 store tz 벽시계. tz는 부여하지 않음(naive).
    """
    start_at = None
    end_at = None
    s_date = start_date or operating_day
    if start_time is not None:
        start_at = datetime.combine(s_date, start_time)
    if end_time is not None:
        if end_date is not None:
            e_date = end_date
        elif start_time is not None and end_time <= start_time:
            e_date = s_date + _timedelta(days=1)
        else:
            e_date = s_date
        end_at = datetime.combine(e_date, end_time)
    return start_at, end_at


def assemble_break_datetime(
    anchor_date: date,
    start_time: time | None,
    break_time: time | None,
) -> datetime | None:
    """[TRANSITION-ONLY] 브레이크 시각을 naive datetime으로 조립합니다.

    근무 시작 날짜(anchor_date)에 앵커하되, 브레이크 시각이 start_time보다 이르면
    오버나잇 근무 내의 브레이크로 보아 +1일. 마이그레이션 백필과 동일 규칙.
    """
    if break_time is None:
        return None
    d = anchor_date
    if start_time is not None and break_time < start_time:
        d = d + _timedelta(days=1)
    return datetime.combine(d, break_time)


def parse_naive_iso(value: str | None) -> datetime | None:
    """ISO 문자열("YYYY-MM-DDTHH:MM")을 naive datetime으로 파싱합니다.

    벽시계 계약(tz 없음). tz-aware가 들어와도 벽시계 성분만 취해 naive로 만든다.
    """
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def format_naive_iso(value: datetime | None) -> str | None:
    """naive datetime → "YYYY-MM-DDTHH:MM" ISO 문자열 (분 단위)."""
    if value is None:
        return None
    return value.strftime("%Y-%m-%dT%H:%M")


def resolve_schedule_instants(
    *,
    start_at: datetime | None,
    end_at: datetime | None,
    work_date: date | None,
    start_time: time | None,
    end_time: time | None,
    tz_name: str,
) -> tuple[datetime | None, datetime | None]:
    """스케줄의 시작/종료를 store tz aware datetime(절대 순간)으로 해석합니다.

    start_at/end_at(naive 벽시계)이 있으면 그것을 store tz로 localize.
    없으면(전환기 미백필 행) 구 combine(work_date, time) + 자정보정으로 폴백.
    근태 비교(instant vs clock_in) 용.
    """
    zi = ZoneInfo(tz_name)
    if start_at is not None:
        s = start_at.replace(tzinfo=zi)
    elif start_time is not None and work_date is not None:
        s = datetime.combine(work_date, start_time, tzinfo=zi)
    else:
        s = None

    if end_at is not None:
        e = end_at.replace(tzinfo=zi)
    elif end_time is not None and work_date is not None:
        e = datetime.combine(work_date, end_time, tzinfo=zi)
        if s is not None and e <= s:
            e = e + _timedelta(days=1)
    else:
        e = None
    return s, e


def net_minutes_from_datetimes(
    start_at: datetime | None,
    end_at: datetime | None,
    break_start_at: datetime | None = None,
    break_end_at: datetime | None = None,
) -> int:
    """datetime 구간에서 순 근무 분을 계산합니다 (자정보정 특수처리 불필요).

    Net work minutes = (end_at − start_at) − (break_end_at − break_start_at).
    날짜가 값에 포함돼 있으므로 overnight `+24h` 보정이 없다.
    """
    if start_at is None or end_at is None:
        return 0
    total = minutes_between(start_at, end_at)
    if break_start_at is not None and break_end_at is not None:
        total -= minutes_between(break_start_at, break_end_at)
    return max(total, 0)


def calculate_cross_midnight_minutes(start_time: time, end_time: time) -> int:
    """자정을 넘는 근무시간을 올바르게 계산합니다.

    Calculate work minutes handling cross-midnight shifts.
    If end_time < start_time, assumes the shift crosses midnight.

    Example: 22:00 → 02:00 = 240 minutes (4 hours)

    Args:
        start_time: 시작 시각 (Shift start time)
        end_time: 종료 시각 (Shift end time)

    Returns:
        int: 근무 분 (Work duration in minutes)
    """
    start_minutes = start_time.hour * 60 + start_time.minute
    end_minutes = end_time.hour * 60 + end_time.minute

    if end_minutes <= start_minutes:
        # Crosses midnight
        return (24 * 60 - start_minutes) + end_minutes
    return end_minutes - start_minutes


# 시간대 형태를 쓰는 설정 키 — 저장 전에 값 형태를 검증한다.
# 여기 없는 키는 검증하지 않는다(기존 키의 자유 형태를 깨지 않기 위함).
DAY_RANGE_SETTING_KEYS: frozenset[str] = frozenset({
    "store.operating_hours",
    "schedule.range",
})


def validate_day_range_setting(key: str, value: object) -> None:
    """시간대 설정 값의 형태를 검증한다. 어긋나면 ValueError.

    **왜 저장 시점에 막는가** — 파서는 형태가 깨진 값을 예외 없이 None(=미설정)으로
    떨어뜨린다. 설정 하나가 리포트 전체를 죽이지 않게 한 의도적 설계인데,
    그 대가로 잘못 저장된 값이 **아무 신호 없이** 기능을 멈춘다.
    실제로 24 초과 표기(`26:00`)가 들어가면 그 매장의 SV 공백 검사가 조용히 사라진다.
    입구에서 거절하는 것이 유일한 방어다.
    """
    if key not in DAY_RANGE_SETTING_KEYS:
        return
    if value is None or not isinstance(value, dict):
        raise ValueError("Value must be an object.")

    def _check_entry(entry: object, where: str) -> None:
        if entry in (None, {}):
            return  # 미설정 — 허용(제한 없음)
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be an object.")
        for field in ("start", "end"):
            raw = entry.get(field)
            if raw is None:
                raise ValueError(f"{where}.{field} is required.")
            if parse_wall_clock_minutes(raw) is None:
                raise ValueError(
                    f"{where}.{field} must be \"HH:MM\" between 00:00 and 23:59"
                    " (24+ notation is not allowed — use end_offset_days)."
                )
        off = entry.get("end_offset_days", 0)
        if off not in (0, 1):
            raise ValueError(f"{where}.end_offset_days must be 0 or 1.")

    _check_entry(value.get("all"), "all")
    per_day = value.get("per_day") or {}
    if not isinstance(per_day, dict):
        raise ValueError("per_day must be an object.")
    for day, entry in per_day.items():
        _check_entry(entry, f"per_day.{day}")

    closed = value.get("closed", [])
    if closed is not None and not isinstance(closed, list):
        raise ValueError("closed must be an array of weekday keys.")
