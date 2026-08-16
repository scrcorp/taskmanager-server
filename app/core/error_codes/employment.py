"""고용 상태 — 퇴사(Offboard)와 휴직(on_leave).

재직 상태는 `org_members.status` 가 진실의 원천이다(active / on_leave / terminated).
여기의 코드는 그 상태 전이가 성립하지 않을 때 쓴다.

설계: docs/99_inbox/2026-08-13-조직계층-재정의.md §6 · D3 · D5
"""

from __future__ import annotations

from app.core.error_codes._registry import domain

EMPLOYMENT = domain("employment")

MEMBERSHIP_NOT_FOUND = EMPLOYMENT.code(
    "MEMBERSHIP_NOT_FOUND",
    404,
    "This person isn't a member of your organization.",
)

EMPLOYEE_NOT_FOUND = EMPLOYMENT.code(
    "EMPLOYEE_NOT_FOUND",
    404,
    "This employee no longer exists.",
)

OFFBOARD_PROVISIONAL = EMPLOYMENT.code(
    "OFFBOARD_PROVISIONAL",
    400,
    "This employee hasn't signed up yet, so there's nothing to offboard.",
    hint="Remove the placeholder instead.",
)

ALREADY_TERMINATED = EMPLOYMENT.code(
    "ALREADY_TERMINATED",
    400,
    "This employee has already been offboarded.",
    hint="Rehire them before putting them on leave.",
)

LEAVE_END_BEFORE_START = EMPLOYMENT.code(
    "LEAVE_END_BEFORE_START",
    400,
    "The return date can't be earlier than the leave start date.",
)

NOT_ON_LEAVE = EMPLOYMENT.code(
    "NOT_ON_LEAVE",
    400,
    "This employee isn't on leave.",
)

RETENTION_NOT_ELAPSED = EMPLOYMENT.code(
    "RETENTION_NOT_ELAPSED",
    400,
    "This record is still within the retention period.",
    hint="Only offboarded employees past the retention window can be anonymized.",
)

LAST_ADMIN = EMPLOYMENT.code(
    "LAST_ADMIN",
    400,
    "This is the last person who can administer this organization.",
    hint="Give someone else the same level of access first.",
)
