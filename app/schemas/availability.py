"""근무가능시간(Work Availability) Pydantic 스키마.

House style: snake_case JSON, alias_generator 없음 (다른 도메인 스키마와 동일).
시간은 "HH:MM" 문자열(5분 그리드; overnight 허용). 상태 3종: off/range/full.
"""

from __future__ import annotations

import re
from datetime import datetime, time

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.availability import AVAILABILITY_STATES

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def validate_5min_grid(value: str | None) -> str | None:
    """"HH:MM" on a 5-minute grid; None/'' pass (optional)."""
    if value is None or value == "":
        return value
    m = _HHMM_RE.match(value)
    if not m:
        raise ValueError("Time must be in HH:MM format.")
    if int(m.group(2)) % 5 != 0:
        raise ValueError("Time must be on a 5-minute boundary.")
    return value


def parse_hhmm(value: str | None) -> time | None:
    if not value:
        return None
    return datetime.strptime(value, "%H:%M").time()


def fmt_hhmm(value: time | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%H:%M")


class AvailabilityDayIn(BaseModel):
    """한 요일의 입력 값."""

    day_of_week: int = Field(..., ge=0, le=6)  # 0=Sun .. 6=Sat
    state: str
    start_time: str | None = None  # "HH:MM"
    end_time: str | None = None

    _v_start = field_validator("start_time")(validate_5min_grid)
    _v_end = field_validator("end_time")(validate_5min_grid)

    @field_validator("state")
    @classmethod
    def _state_valid(cls, v: str) -> str:
        if v not in AVAILABILITY_STATES:
            raise ValueError(f"state must be one of {AVAILABILITY_STATES}")
        return v

    @model_validator(mode="after")
    def _check_times(self) -> "AvailabilityDayIn":
        if self.state == "range":
            if not self.start_time or not self.end_time:
                raise ValueError("range requires start_time and end_time")
            if parse_hhmm(self.end_time) == parse_hhmm(self.start_time):
                raise ValueError("start_time and end_time cannot be the same")
        else:  # off / full → 시간 없음
            self.start_time = None
            self.end_time = None
        return self


class AvailabilityWeekUpdate(BaseModel):
    """주간 저장 요청. days 에 담긴 요일이 그 주의 전부(빠진 요일은 Off)."""

    days: list[AvailabilityDayIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_dupe_dow(self) -> "AvailabilityWeekUpdate":
        seen = [d.day_of_week for d in self.days]
        if len(seen) != len(set(seen)):
            raise ValueError("duplicate day_of_week in days")
        return self


class AvailabilityDayOut(BaseModel):
    day_of_week: int
    state: str
    start_time: str | None = None
    end_time: str | None = None


class AvailabilityMemberOut(BaseModel):
    """한 스태프의 주간 근무가능시간 (7일 모두, 미설정 요일은 off)."""

    user_id: str
    full_name: str | None = None
    days: list[AvailabilityDayOut]
    updated_at: datetime | None = None


class AvailabilityHistoryOut(BaseModel):
    day_of_week: int | None = None
    source: str
    snapshot: dict
    prev: dict | None = None
    description: str | None = None
    actor_id: str | None = None
    actor_name: str | None = None  # 변경자 이름 (users.full_name)
    created_at: datetime


class AvailabilityDetailOut(BaseModel):
    """콘솔 개별 조회 — 주간 + 수정 이력."""

    member: AvailabilityMemberOut
    history: list[AvailabilityHistoryOut]


class MyAvailabilityOut(BaseModel):
    """앱/셀프 조회 — 읽기 전용 + 셀프 편집 허용 여부."""

    days: list[AvailabilityDayOut]
    can_edit: bool  # 미설정(최초 1회)일 때만 True — 당분간 정책
    updated_at: datetime | None = None


# ── 프리셋 (기본 세팅) ──────────────────────────────────────
class PresetOut(BaseModel):
    """근무가능시간 프리셋. 빌트인 system + org custom 을 동일 형태로 반환."""

    id: str
    name: str
    days: list[AvailabilityDayOut]
    is_system: bool


class PresetCreate(BaseModel):
    """org custom 프리셋 생성. days 는 주간 저장과 동일한 검증기를 재사용."""

    name: str = Field(..., min_length=1, max_length=100)
    days: list[AvailabilityDayIn] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_trim(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @model_validator(mode="after")
    def _no_dupe_dow(self) -> "PresetCreate":
        seen = [d.day_of_week for d in self.days]
        if len(seen) != len(set(seen)):
            raise ValueError("duplicate day_of_week in days")
        return self
