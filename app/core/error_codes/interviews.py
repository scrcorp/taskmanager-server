"""면접 일정 — 지원자 공개 링크(`app/api/app/interview_schedule.py`)와 콘솔 양쪽이 쓴다.

같은 코드를 두 라우터가 쓰는 것은 정상이다(같은 뜻). 도메인이 하나이므로 여기 한 번만 선언한다.
"""

from __future__ import annotations

from app.core.error_codes._registry import domain

INTERVIEWS = domain("interviews")

# ── 지원자 링크 ─────────────────────────────────────────
INVALID_TOKEN = INTERVIEWS.legacy("invalid_token", 400, "This scheduling link is not valid.")
TOKEN_EXPIRED = INTERVIEWS.legacy(
    "token_expired",
    410,
    "This scheduling link has expired.",
    hint="Please contact the store.",
)
NOT_IN_INTERVIEW = INTERVIEWS.legacy(
    "not_in_interview", 409, "This application is not in the interview stage."
)
ALREADY_CONFIRMED = INTERVIEWS.legacy(
    "already_confirmed", 409, "Your interview is already confirmed."
)
NO_PICKS = INTERVIEWS.legacy("no_picks", 400, "Pick at least one time.")
TOO_MANY = INTERVIEWS.legacy("too_many", 400, "You picked more times than allowed.")

# ── 슬롯 ────────────────────────────────────────────────
INVALID_SLOT = INTERVIEWS.legacy("invalid_slot", 400, "Some times are no longer available.")
SLOT_NOT_FOUND = INTERVIEWS.legacy("slot_not_found", 404, "This time slot no longer exists.")
SLOT_TAKEN = INTERVIEWS.legacy(
    "slot_taken", 409, "One of those times was just booked. Please pick again."
)
SLOT_CONFIRMED = INTERVIEWS.legacy(
    "slot_confirmed",
    400,
    "Cancel the confirmed interview before deleting this slot.",
)
INVALID_TIME = INTERVIEWS.legacy("invalid_time", 400, "This time value is not valid.")
INVALID_INTERVIEWER = INTERVIEWS.legacy(
    "invalid_interviewer", 400, "This interviewer is not available for this store."
)
