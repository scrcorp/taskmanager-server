"""연락처(Contacts) — 조직 전화번호부의 도메인 에러 코드.

계약: `docs/99_inbox/2026-08-14-연락처-API계약.md` §6.
계약 문서는 lower_snake `error_code` 로 적혀 있으나, 서버의 실제 규약은
에러 봉투(`app/core/error_envelope.py`) + 이 레지스트리이고 **신규 코드는 UPPER_SNAKE**
이므로 아래 이름을 정본으로 삼는다. 콘솔은 `error.code` 로 분기한다.

대응표 (계약 문서 → 실제 코드)
    validation_error     → CONTACT_VALIDATION_ERROR
    reason_required      → CONTACT_REASON_REQUIRED
    store_forbidden      → CONTACT_STORE_FORBIDDEN
    permission_denied    → (공통 require_permission 403 — 문자열 detail 그대로)
    not_your_request     → CONTACT_NOT_YOUR_REQUEST
    request_not_pending  → CONTACT_REQUEST_NOT_PENDING
    contact_deleted      → CONTACT_DELETED
    404 (문자열)          → CONTACT_NOT_FOUND / CONTACT_REQUEST_NOT_FOUND
"""

from __future__ import annotations

from app.core.error_codes._registry import domain

CONTACTS = domain("contacts")

CONTACT_NOT_FOUND = CONTACTS.code(
    "CONTACT_NOT_FOUND",
    404,
    "This contact is not available.",
    hint="It may have been deleted, or you may not have access to it.",
)

CONTACT_REQUEST_NOT_FOUND = CONTACTS.code(
    "CONTACT_REQUEST_NOT_FOUND",
    404,
    "This request is not available.",
    hint="It may have been withdrawn, or you may not have access to it.",
)

CONTACT_VALIDATION_ERROR = CONTACTS.code(
    "CONTACT_VALIDATION_ERROR",
    400,
    "Some contact details are not valid.",
    hint="Check the highlighted fields and try again.",
)

CONTACT_REASON_REQUIRED = CONTACTS.code(
    "CONTACT_REASON_REQUIRED",
    400,
    "A reason is required for this change.",
    hint="Enter a reason before saving.",
)

CONTACT_STORE_FORBIDDEN = CONTACTS.code(
    "CONTACT_STORE_FORBIDDEN",
    403,
    "You do not have access to that store.",
    hint="Pick a store you manage, or share the contact with all stores.",
)

CONTACT_PERMISSION_DENIED = CONTACTS.code(
    "CONTACT_PERMISSION_DENIED",
    403,
    "You do not have permission to make this change to contacts.",
    hint="You can submit a request instead, and someone with access will review it.",
)

CONTACT_NOT_YOUR_REQUEST = CONTACTS.code(
    "CONTACT_NOT_YOUR_REQUEST",
    403,
    "Only the person who submitted this request can cancel it.",
)

CONTACT_REQUEST_NOT_PENDING = CONTACTS.code(
    "CONTACT_REQUEST_NOT_PENDING",
    409,
    "This request was already handled.",
    hint="Refresh to see its current status.",
)

CONTACT_DELETED = CONTACTS.code(
    "CONTACT_DELETED",
    409,
    "This contact was deleted while the request was pending.",
    hint="Reject the request instead of approving it.",
)
