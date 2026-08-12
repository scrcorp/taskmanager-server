"""채용 — 지원서 제출·양식·단계 이동.

여기 코드 다수가 지금 `{"code": ...}` 만 있고 **문구가 없다**(E1-d 의 59곳 중 큰 덩어리).
문구를 여기 적어 두면, 그 지점을 helper 로 옮기는 순간 사용자가 볼 문장이 생긴다.
"""

from __future__ import annotations

from app.core.error_codes._registry import domain

HIRING = domain("hiring")

# ── 지원서 ──────────────────────────────────────────────
APPLICATION_NOT_FOUND = HIRING.legacy(
    "application_not_found", 404, "This application no longer exists."
)
INVALID_APPLICATION_ID = HIRING.legacy(
    "invalid_application_id", 400, "This application reference is not valid."
)
CANNOT_WITHDRAW = HIRING.legacy(
    "cannot_withdraw", 400, "This application can no longer be withdrawn."
)
NOT_PENDING_FORM = HIRING.legacy(
    "not_pending_form", 400, "This application is not in form-filling state."
)
NOT_YET_SUBMITTED = HIRING.legacy(
    "not_yet_submitted", 400, "Wait for the applicant to submit the form."
)

# ── 지원 양식 ───────────────────────────────────────────
INVALID_FORM = HIRING.legacy("invalid_form", 400, "This form could not be saved.")
INVALID_FORM_ID = HIRING.legacy("invalid_form_id", 400, "This form reference is not valid.")
FORM_NOT_FOUND = HIRING.legacy("form_not_found", 404, "This form no longer exists.")
FORM_NOT_PUBLISHED = HIRING.legacy("form_not_published", 400, "This form is not published yet.")
FORM_STORE_MISMATCH = HIRING.legacy(
    "form_store_mismatch", 400, "This form belongs to a different store."
)
FORM_ID_REQUIRED = HIRING.legacy("form_id_required", 400, "Choose which form to fill in.")
NO_DRAFT = HIRING.legacy("no_draft", 400, "No draft to publish.")

# ── 제출 내용 검증 ──────────────────────────────────────
MISSING_REQUIRED_ANSWER = HIRING.legacy(
    "missing_required_answer", 400, "Some required questions are not answered."
)
MISSING_REQUIRED_ATTACHMENT = HIRING.legacy(
    "missing_required_attachment", 400, "Some required attachments are missing."
)
INVALID_ATTACHMENT_MIME = HIRING.legacy(
    "invalid_attachment_mime", 400, "This attachment type is not accepted."
)
INVALID_ACCEPT = HIRING.legacy("invalid_accept", 400, "This attachment rule is not valid.")
UNKNOWN_SLOT = HIRING.legacy("unknown_slot", 400, "This question no longer exists on the form.")

# ── 단계 이동 / 채용 확정 ───────────────────────────────
INVALID_STAGE = HIRING.legacy("invalid_stage", 400, "This stage change is not allowed.")
USE_HIRE_ENDPOINT = HIRING.legacy(
    "use_hire_endpoint", 400, "Use the hire action to move this applicant to hired."
)
ALREADY_HIRED = HIRING.legacy("already_hired", 400, "This applicant is already hired.")
NOT_HIRED = HIRING.legacy("not_hired", 400, "Only hired applications can be unhired.")
NO_STAFF_ROLE = HIRING.legacy(
    "no_staff_role",
    400,
    "Staff role not configured.",
    hint="Create a staff role before hiring.",
)
INVALID_USER_ID = HIRING.legacy("invalid_user_id", 400, "This employee reference is not valid.")
