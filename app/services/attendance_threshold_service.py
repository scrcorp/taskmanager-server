"""근태 판정 임계값 해석기 — 세 임계값을 읽는 **유일한** 경로.

Attendance threshold resolver: store → org → registry default → code fallback.

세 임계값(late buffer / early clock-in / early leave)은 전부 매장 설정에서 온다.
예전엔 파일마다 각자 `resolve_setting` 을 부르고 각자 `5` 를 fallback 으로 박아
두어서, 같은 근태 한 건이 경로별로 다른 판정을 받았다(키오스크 0분, 콘솔 5분).
읽기를 여기 한 곳으로 모아 두면 기본값을 바꿀 때 한 군데만 고치면 된다.

fallback 상수는 `app/utils/attendance_judgement.py` 가 원본이고, 그 값은
`app/seeds/settings_seed.py` 의 default_value 와 같아야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.attendance_judgement import (
    DEFAULT_EARLY_CLOCK_IN_THRESHOLD_MINUTES,
    DEFAULT_EARLY_LEAVE_THRESHOLD_MINUTES,
    DEFAULT_LATE_BUFFER_MINUTES,
    SETTING_KEY_EARLY_CLOCK_IN_THRESHOLD,
    SETTING_KEY_EARLY_LEAVE_THRESHOLD,
    SETTING_KEY_LATE_BUFFER,
)
from app.utils.settings_resolver import SettingNotRegisteredError, resolve_setting


@dataclass(frozen=True)
class AttendanceThresholds:
    """한 매장의 판정 임계값 묶음 (분 단위)."""

    late_buffer: int
    early_clock_in: int
    early_leave: int


async def _resolve_minutes(
    db: AsyncSession,
    *,
    key: str,
    default: int,
    organization_id: UUID | None,
    store_id: UUID | None,
) -> int:
    """설정 키를 분(int)으로 해석. 미등록/미설정/파싱실패는 전부 default.

    설정 하나가 출퇴근 자체를 막으면 안 되므로 예외를 삼킨다 — 다만 그 대가로
    잘못된 값이 조용히 기본값으로 떨어지므로, default 는 반드시 시드 기본값과 같아야 한다.
    """
    if organization_id is None:
        return default
    try:
        raw = await resolve_setting(
            db,
            key=key,
            organization_id=organization_id,
            store_id=store_id,
        )
        return int(raw) if raw is not None else default
    except (SettingNotRegisteredError, TypeError, ValueError):
        return default


async def resolve_late_buffer(
    db: AsyncSession,
    *,
    organization_id: UUID | None,
    store_id: UUID | None = None,
) -> int:
    """스케줄 시작 후 지각으로 보지 않는 유예 분."""
    return await _resolve_minutes(
        db,
        key=SETTING_KEY_LATE_BUFFER,
        default=DEFAULT_LATE_BUFFER_MINUTES,
        organization_id=organization_id,
        store_id=store_id,
    )


async def resolve_early_clock_in_threshold(
    db: AsyncSession,
    *,
    organization_id: UUID | None,
    store_id: UUID | None = None,
) -> int:
    """스케줄 시작 전 사유 없이 출근할 수 있는 분."""
    return await _resolve_minutes(
        db,
        key=SETTING_KEY_EARLY_CLOCK_IN_THRESHOLD,
        default=DEFAULT_EARLY_CLOCK_IN_THRESHOLD_MINUTES,
        organization_id=organization_id,
        store_id=store_id,
    )


async def resolve_early_leave_threshold(
    db: AsyncSession,
    *,
    organization_id: UUID | None,
    store_id: UUID | None = None,
) -> int:
    """스케줄 종료 전 사유 없이 퇴근할 수 있는 분."""
    return await _resolve_minutes(
        db,
        key=SETTING_KEY_EARLY_LEAVE_THRESHOLD,
        default=DEFAULT_EARLY_LEAVE_THRESHOLD_MINUTES,
        organization_id=organization_id,
        store_id=store_id,
    )


async def resolve_thresholds(
    db: AsyncSession,
    *,
    organization_id: UUID | None,
    store_id: UUID | None = None,
) -> AttendanceThresholds:
    """세 임계값을 한 번에. 판정 직전에 개별 조회를 늘리지 않기 위한 묶음."""
    return AttendanceThresholds(
        late_buffer=await resolve_late_buffer(
            db, organization_id=organization_id, store_id=store_id
        ),
        early_clock_in=await resolve_early_clock_in_threshold(
            db, organization_id=organization_id, store_id=store_id
        ),
        early_leave=await resolve_early_leave_threshold(
            db, organization_id=organization_id, store_id=store_id
        ),
    )


async def resolve_late_buffer_cached(
    db: AsyncSession,
    cache: dict,
    *,
    organization_id: UUID | None,
    store_id: UUID | None = None,
) -> int:
    """목록 루프용 — (org, store) 별 1회만 조회하는 저비용 캐시.

    `resolve_setting` 은 호출마다 registry + org(+store) 를 각각 조회한다. 목록
    응답이 레코드마다 이걸 부르면 그대로 N+1 이 된다(실제로 attendance 목록이 그랬다).
    캐시는 호출자가 만들어 넘기는 dict — 요청 하나의 수명만 갖는다.
    """
    ck = (organization_id, store_id)
    if ck not in cache:
        cache[ck] = await resolve_late_buffer(
            db, organization_id=organization_id, store_id=store_id
        )
    return cache[ck]
