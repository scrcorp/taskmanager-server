"""고정 근무(Fixed Schedule) 패턴 — `/schedules/patterns` 전용 HTTP 에러 코드.

계약: docs/99_inbox/2026-08-20-고정근무-구현계약.md §3-3, §4.

왜 `schedule` 도메인이 아닌가 — `schedule` 도메인은 `schedule_codes.py`(3-repo 검증 항목
계약)를 **그대로 흡수**하는 곳이고, 테스트가 그 집합과의 일치를 강제한다. 여기 6개는
errors/warnings 배열에 실리는 검증 항목이 아니라 패턴 엔드포인트가 단독으로 던지는 HTTP
에러라 별도 도메인으로 둔다. params 는 평탄하게 나간다:
예) `raise PATTERN_BLOCK_OVERLAP(blocks=[0, 2], dow=1)`

쓰는 법::

    from app.core.error_codes.fixed_schedule import PATTERN_OVERLAP_EXISTING
    raise PATTERN_OVERLAP_EXISTING(overlaps=[g.model_dump(mode="json") for g in groups])
"""

from __future__ import annotations

from app.core.error_codes._registry import domain

FIXED_SCHEDULE = domain("fixed_schedule")



PATTERN_BLOCK_OVERLAP = FIXED_SCHEDULE.code(
    "PATTERN_BLOCK_OVERLAP",
    400,
    "Two blocks in this fixed schedule overlap on the same day of the week.",
    hint="Change the times or uncheck the day on one of the highlighted blocks, then save again.",
)
"""① 같은 설정창 안 블록끼리 같은 요일·시간 겹침. params: {blocks: [index...], dow}"""

PATTERN_OVERLAP_EXISTING = FIXED_SCHEDULE.code(
    "PATTERN_OVERLAP_EXISTING",
    409,
    "This fixed schedule overlaps an existing fixed schedule for the same person and store.",
    hint="Choose how to resolve it: move the existing schedule earlier, replace it, or change the days.",
)
"""② 다른 그룹 패턴과 겹침(같은 user·store·요일 ∧ 시간 ∧ 기간 교차). params: {overlaps: [PatternGroupOut...]}"""

PATTERN_OUTSIDE_AVAILABILITY = FIXED_SCHEDULE.code(
    "PATTERN_OUTSIDE_AVAILABILITY",
    400,
    "This fixed schedule falls outside the employee's work availability.",
    hint="Pick a time inside their availability, or update their availability first.",
)
"""④ staff_availability 가 off 거나 range 밖. 행 없음 = 제약 없음. params: {dow, block}"""

PATTERN_MOVE_INTO_PAST = FIXED_SCHEDULE.code(
    "PATTERN_MOVE_INTO_PAST",
    409,
    "A fixed schedule cannot be moved to start in the past.",
    hint="Use a smaller shift so the start date is today or later.",
)
"""move_group 결과 start_date < today. params: {start_date, delta_days}"""

PATTERN_GROUP_STARTED = FIXED_SCHEDULE.code(
    "PATTERN_GROUP_STARTED",
    409,
    "This fixed schedule has already started, so its dates cannot be moved.",
    hint="Edit the schedule instead — the change will apply from today onward.",
)
"""진행 중 그룹(min(start_date) <= today)에 move 시도. params: {group_id, start_date}"""

PATTERN_REVERT_NOT_OVERRIDDEN = FIXED_SCHEDULE.code(
    "PATTERN_REVERT_NOT_OVERRIDDEN",
    409,
    "This day has not been changed from its fixed schedule, so there is nothing to revert.",
    hint="Only edited days can be reverted. Deleted days cannot be restored.",
)
"""revert_to_pattern 대상이 overridden 이 아니거나 deleted. params: {schedule_id, status}"""


# ─── server-patterns 가 추가 (2026-08-20): 서비스 내부 raise 는 전부 레지스트리 코드라야 한다(G7 가드) ───

PATTERN_NOT_FOUND = FIXED_SCHEDULE.code(
    "PATTERN_NOT_FOUND",
    404,
    "This fixed schedule no longer exists.",
    hint="Refresh the page — it may have been deleted by someone else.",
)
"""group_id / pattern_id 가 이 org 에 없다. params: {group_id | pattern_id}"""

PATTERN_NO_OCCURRENCE = FIXED_SCHEDULE.code(
    "PATTERN_NO_OCCURRENCE",
    400,
    "This fixed schedule has no shift on that day.",
    hint="It is not one of its days, it is outside its period, or the employee is not assignable that day.",
)
"""occurrence 편집/삭제/revert 대상 날짜가 펼치기 결과에 없다. params: {pattern_id, date}"""

PATTERN_BLOCK_PERIOD_INVALID = FIXED_SCHEDULE.code(
    "PATTERN_BLOCK_PERIOD_INVALID",
    400,
    "A block ends before it starts.",
    hint="Move the block's end date later, or remove the block.",
)
"""블록별 기간이 공통 기간/오늘과 합쳐진 뒤 until < start. params: {start_date, until_date}"""

PATTERN_MOVE_PAST_END = FIXED_SCHEDULE.code(
    "PATTERN_MOVE_PAST_END",
    400,
    "The existing fixed schedule ends before the new start date, so it cannot be moved there.",
    hint="Choose Replace instead, or pick an earlier start date.",
)
"""gate=move 인데 기존 그룹의 until_date 가 새 start_date 보다 앞. params: {group_id, until_date, start_date}"""

PATTERN_SUBJECT_IMMUTABLE = FIXED_SCHEDULE.code(
    "PATTERN_SUBJECT_IMMUTABLE",
    400,
    "A fixed schedule cannot be moved to another person or store.",
    hint="Create a new fixed schedule for them instead.",
)
"""update_group 에서 user_id / store_id 변경 시도. params: {group_id}"""

PATTERN_DAY_REMOVED = FIXED_SCHEDULE.code(
    "PATTERN_DAY_REMOVED",
    400,
    "This day was removed from the fixed schedule and cannot be edited.",
    hint="Create a one-time schedule for that day instead.",
)
"""deleted 슬롯에 edit 시도. params: {schedule_id, date}"""

PATTERN_PATCH_REQUIRED = FIXED_SCHEDULE.code(
    "PATTERN_PATCH_REQUIRED",
    400,
    "Provide the changes to apply to this day.",
)
"""action=edit 인데 patch 가 없다(스키마가 먼저 막지만 서비스 직접 호출 대비)."""


# ─── 오케스트레이터 추가 (2026-08-20 audit F1): schedule_service 도장 쓰기 통로 전용 ───

PATTERN_TARGET_NOT_FOUND = FIXED_SCHEDULE.code(
    "PATTERN_TARGET_NOT_FOUND",
    404,
    "The schedule for this fixed-schedule day no longer exists.",
    hint="Refresh the page — it may have been deleted or moved.",
)
"""set_pattern_stamp / set_pattern_overridden 대상 schedule 행이 이 org 에 없다. params: {schedule_id}"""

PATTERN_NOT_STAMPED = FIXED_SCHEDULE.code(
    "PATTERN_NOT_STAMPED",
    400,
    "This is a one-time schedule, not part of a fixed schedule.",
    hint="Only schedules created from a fixed schedule can be marked as overridden.",
)
"""도장이 없는 행에 overridden=True 를 켜려 했다. params: {schedule_id}"""
