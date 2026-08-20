"""급여 확정 — 콘솔이 코드로 분기 렌더하는 409 두 종류.

문자열은 `app/schemas/payroll.py` 의 `CODE_ALREADY_CONFIRMED` / `CODE_CLOSE_GATES_FAILED` 와
같아야 한다. 두 곳이 갈리면 콘솔이 게이트 목록을 못 그린다.
"""

from __future__ import annotations

from app.core.error_codes._registry import domain

PAYROLL = domain("payroll")

PAY_PERIOD_ALREADY_CONFIRMED = PAYROLL.legacy(
    "pay_period_already_confirmed",
    409,
    "This pay period is already confirmed.",
)

PAYROLL_CLOSE_GATES_FAILED = PAYROLL.legacy(
    "payroll_close_gates_failed",
    409,
    "Some close gates failed. Resolve every issue below, then confirm again.",
)
"""params: gates(list) — 콘솔이 목록으로 렌더한다."""

# ── group 스코프 전환 (2026-08-19, D1~D6) ─────────────────────────────

PAYROLL_GROUP_HAS_NO_STORES = PAYROLL.code(
    "PAYROLL_GROUP_HAS_NO_STORES",
    400,
    "This group has no stores.",
    hint="Assign at least one store to the group before running payroll.",
)

PAYROLL_GROUP_TIMEZONE_MISMATCH = PAYROLL.code(
    "PAYROLL_GROUP_TIMEZONE_MISMATCH",
    400,
    "Stores in this group use different timezones: {zones}.",
    hint="Payroll needs a single business-day boundary per group — align the store timezones, then retry.",
)

PAYROLL_SCOPE_MISSING = PAYROLL.code(
    "PAYROLL_SCOPE_MISSING",
    400,
    "This pay period's store or group no longer exists.",
)

BONUS_EFFECTIVE_DATE_INVALID = PAYROLL.code(
    "BONUS_EFFECTIVE_DATE_INVALID",
    400,
    "Bonus rate changes must take effect on the 1st or 16th.",
    hint="Pick a pay-period start date (1st or 16th) as the effective date.",
)
