"""Break-end 시간/사유 정책 — Stage J pure helper.

policy:
  - paid_10min  : 최소 10분. 그 이하면 reject.
  - unpaid_meal : 최소 30분. 그 이하면 reject. 35분 이상이면 reason 필수.
  - 그 외 type  : 검증 없음 (None 반환).

호출 측 (`attendance_device_service.perform_clock_action`) 은 반환 메시지가
None 이 아니면 BadRequestError 로 raise.
"""

from __future__ import annotations

from app.models.attendance_break import normalize_break_type


def validate_break_end(
    break_type: str,
    elapsed_minutes: int,
    reason: str | None,
) -> str | None:
    """break_end 시도 시 정책 위반 여부 검사. 위반이면 사용자에게 보일 메시지, OK면 None."""
    normalized = normalize_break_type(break_type)
    if normalized == "paid_10min":
        if elapsed_minutes < 10:
            remaining = 10 - elapsed_minutes
            return f"End Break available after {remaining}m more (10-minute minimum)"
        return None
    if normalized == "unpaid_meal":
        if elapsed_minutes < 30:
            remaining = 30 - elapsed_minutes
            return f"End Break available after {remaining}m more (30-minute minimum)"
        if elapsed_minutes >= 35 and not (reason and reason.strip()):
            return "Meal break over 35 minutes requires a reason."
        return None
    return None
