"""타임존 유틸리티 — 매장/조직 타임존 해석 헬퍼.

Timezone utility — helpers for resolving store/organization timezone.
Includes day boundary logic for determining work_date.
"""

from datetime import date, datetime, time
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization, Store

DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_DAY_START_TIME = "06:00"

# Weekday name mapping (Python weekday() -> JSONB key)
_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


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
