"""고정 근무(Fixed Schedule) 패턴 API 스키마.

계약: docs/99_inbox/2026-08-20-고정근무-구현계약.md §4 (`/schedules/patterns`).

용어 — UI 는 `Fixed` ↔ `One-time`, 코드/DB 는 `pattern`. "flexible" 금지.
요일 — `byday` 는 0=Sun .. 6=Sat (일요일 시작). 파이썬 weekday(0=Mon) 아님.
시각 — "HH:MM" 문자열(store tz 벽시계), 5분 단위(`SCHEDULE_STEP_MINUTES`).
       패턴은 신규 데이터라 레거시 비배수 값이 없으므로 스키마에서 바로 grid 검증한다
       (일반 스케줄의 D7 예외 — "바뀐 값만 검사" — 는 여기 해당 없음).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.schedule import ScheduleUpdate, validate_grid

# 0=Sun .. 6=Sat
BYDAY_MIN = 0
BYDAY_MAX = 6


def _validate_byday(value: list[int]) -> list[int]:
    """0..6 범위·중복 없음·비어 있지 않음. 정렬해서 돌려준다(비교·저장 안정성)."""
    if not value:
        raise ValueError("Select at least one day of the week.")
    bad = [d for d in value if not (BYDAY_MIN <= d <= BYDAY_MAX)]
    if bad:
        raise ValueError(f"Days of week must be between 0 (Sun) and 6 (Sat); got {bad}.")
    if len(set(value)) != len(value):
        raise ValueError("Days of week must not repeat.")
    return sorted(value)


def _validate_hhmm(value: str | None) -> str | None:
    """HH:MM + 5분 grid. `validate_grid` 가 둘 다 본다."""
    return validate_grid(value)


# ─── Block ───────────────────────────────────────────


class PatternBlockIn(BaseModel):
    """설정창의 블록 1개 → `staff_work_patterns` 행 1개.

    `start_date` / `until_date` 는 그룹 공통값을 블록별로 덮어쓸 때만 보낸다
    ("Different period" 토글). 없으면 `PatternGroupIn` 의 공통값을 쓴다.
    """

    start_time: str = Field(..., description='"HH:MM" store tz wall clock')
    end_time: str = Field(..., description='"HH:MM"; overnight (end < start) allowed')
    break_start_time: str | None = None
    break_end_time: str | None = None
    work_role_id: str | None = None
    byday: list[int] = Field(..., description="0=Sun .. 6=Sat")
    start_date: date | None = None
    until_date: date | None = None

    @field_validator("start_time", "end_time", "break_start_time", "break_end_time")
    @classmethod
    def _hhmm(cls, v: str | None) -> str | None:
        return _validate_hhmm(v)

    @field_validator("byday")
    @classmethod
    def _byday(cls, v: list[int]) -> list[int]:
        return _validate_byday(v)

    @model_validator(mode="after")
    def _pairs(self) -> "PatternBlockIn":
        if self.start_time == self.end_time:
            raise ValueError("End time must be different from start time.")
        if (self.break_start_time is None) != (self.break_end_time is None):
            raise ValueError("Break start and end must be provided together.")
        if self.start_date and self.until_date and self.until_date < self.start_date:
            raise ValueError("End date must be on or after the start date.")
        return self


class PatternBlockOut(BaseModel):
    """저장된 블록 1개 (`staff_work_patterns` 행). `id` 가 곧 `pattern_id`."""

    id: str
    work_role_id: str | None = None
    work_role_name: str | None = None
    rrule: str
    byday: list[int]
    start_time: str
    end_time: str
    break_start_time: str | None = None
    break_end_time: str | None = None
    start_date: date
    until_date: date | None = None


# ─── Group ───────────────────────────────────────────


class PatternGroupIn(BaseModel):
    """한 설정창 저장 = 그룹 1개. 생성(POST)과 전체 교체(PATCH) 모두 이 모양."""

    user_id: str
    store_id: str
    start_date: date
    # None = 무기한
    until_date: date | None = None
    blocks: list[PatternBlockIn] = Field(..., min_length=1)
    # ② 기존 그룹과 겹칠 때의 처리. 미지정이면 409 PATTERN_OVERLAP_EXISTING(후보 동봉).
    #   move    — 기존 그룹의 start_date 만 옮기고 신규는 만들지 않는다
    #   replace — 기존 그룹을 삭제하고 새로 만든다
    gate: Literal["move", "replace"] | None = None

    @model_validator(mode="after")
    def _period(self) -> "PatternGroupIn":
        if self.until_date is not None and self.until_date < self.start_date:
            raise ValueError("End date must be on or after the start date.")
        return self


class PatternGroupOut(BaseModel):
    """그룹 단위 응답. `start_date`/`until_date` 는 블록 전체의 min/max(무기한 블록이 있으면 None)."""

    group_id: str
    user_id: str
    user_name: str | None = None
    store_id: str
    store_name: str | None = None
    start_date: date
    until_date: date | None = None
    blocks: list[PatternBlockOut]
    created_at: datetime


# ─── Validate (저장 없이) ────────────────────────────


class PatternIssue(BaseModel):
    """검증 항목 하나 — `{code, params}`. 문구는 클라가 code 로 구성한다.

    코드: PATTERN_BLOCK_OVERLAP(params: blocks=[index...], dow) /
          PATTERN_OUTSIDE_AVAILABILITY(params: dow, block) — `app/core/error_codes/schedule.py`.
    """

    code: str
    params: dict = {}


class PatternValidateOut(BaseModel):
    """`POST /schedules/patterns/validate` — ①④ 는 errors, ② 는 overlaps(기존 그룹 후보)."""

    errors: list[PatternIssue] = []
    overlaps: list[PatternGroupOut] = []


# ─── Occurrence / Move ───────────────────────────────


class OccurrenceActionIn(BaseModel):
    """virtual 한 칸을 실 행으로 만든다 (`POST /schedules/patterns/{pattern_id}/occurrences/{date}`).

    edit   — 실체화 후 `patch` 적용, `pattern_overridden=True`
    delete — 실체화 후 soft delete(`status='deleted'`), 슬롯 점유 유지
    """

    action: Literal["edit", "delete"]
    patch: ScheduleUpdate | None = None

    @model_validator(mode="after")
    def _patch_required_for_edit(self) -> "OccurrenceActionIn":
        if self.action == "edit" and self.patch is None:
            raise ValueError("Provide the changes to apply when action is 'edit'.")
        return self


class MoveGroupIn(BaseModel):
    """그룹 전체 기간을 `delta_days` 만큼 옮긴다(음수 = 앞당김). 시작 전 그룹만."""

    delta_days: int
