"""리포트 열람 — 직급 기반 가시성에서 막힌 경우.

리포트는 "자기 것 + 자기보다 아래 직급이 쓴 것" 만 보인다(SV 는 다른 SV/GM 것을,
GM 은 다른 GM 것을 못 본다). 목록에서 가려진 건을 id 로 직접 열면 이 코드가 나간다 —
'없음(404)' 이 아니라 '권한 없음(403)' 인 이유는, 존재 자체는 작성자·상급자에게
공개된 사실이고 클라가 재시도/재로그인으로 풀 수 있는 상황이 아니기 때문이다.
"""

from __future__ import annotations

from app.core.error_codes._registry import domain

REPORTS = domain("reports")

REPORT_NOT_VISIBLE = REPORTS.code(
    "REPORT_NOT_VISIBLE",
    403,
    "You do not have access to this report.",
    hint="Only the author and higher-ranked managers can open it.",
)


# ── Issue sharing / notification recipients ────────────────────────
# 공유 설정은 조용히 실패하면 안 된다 — 작성자는 공유했다고 믿는데 아무도 못 보는 상태가
# 되기 때문이다. 그래서 모르는 값·조직 밖 사용자는 400 으로 되돌린다.

ISSUE_VISIBILITY_SCOPE_INVALID = REPORTS.code(
    "ISSUE_VISIBILITY_SCOPE_INVALID",
    400,
    "That sharing option is not available.",
    hint="Choose one of: default, managers, store_all.",
)

ISSUE_RECIPIENT_IDS_INVALID = REPORTS.code(
    "ISSUE_RECIPIENT_IDS_INVALID",
    400,
    "The recipient list is not in a readable format.",
    hint="Reload the recipient list and pick the people again.",
)

ISSUE_RECIPIENT_NOT_IN_ORG = REPORTS.code(
    "ISSUE_RECIPIENT_NOT_IN_ORG",
    400,
    "Some selected people are no longer active members of this organization.",
    hint="Remove them from the recipient list and submit again.",
)

ISSUE_RECIPIENTS_TARGET_REQUIRED = REPORTS.code(
    "ISSUE_RECIPIENTS_TARGET_REQUIRED",
    400,
    "A store or a report is required to look up recipients.",
    hint="Send store_id (new issue) or report_id (existing issue).",
)

ISSUE_RECIPIENTS_STORE_MISMATCH = REPORTS.code(
    "ISSUE_RECIPIENTS_STORE_MISMATCH",
    400,
    "The requested store does not match the report's store.",
    hint="Send report_id alone, or pair it with its own store.",
)

ISSUE_RECIPIENTS_UNAVAILABLE = REPORTS.code(
    "ISSUE_RECIPIENTS_UNAVAILABLE",
    400,
    "This report does not have notification recipients.",
    hint="Only issue reports tied to a store have recipients.",
)


# ── 스케줄 일일 리포트 설정 검증 ────────────────────────
# 값이 깨진 채 저장되면 파서가 조용히 버려서 리포트가 신호 없이 멈춘다.
# 그래서 저장 입구에서 막고, 어떤 항목이 잘못됐는지 그대로 돌려준다.

SCHEDULE_REPORT_RECIPIENTS_INVALID = REPORTS.code(
    "SCHEDULE_REPORT_RECIPIENTS_INVALID",
    400,
    "That is not a valid email address list.",
    hint="Use comma separated email addresses, or leave it empty to stop sending.",
)

SCHEDULE_REPORT_TIMES_INVALID = REPORTS.code(
    "SCHEDULE_REPORT_TIMES_INVALID",
    400,
    "Send times must be hours between 0 and 23.",
    hint="Use comma separated hours like 7,15,22 — or leave it empty to stop sending.",
)


# ── 이슈 커스텀 필드 입력 검증 ────────────────────────
# 필드 정의는 매장이 직접 만든다. 그래서 실패 메시지에는 **어떤 항목이 왜 막혔는지**가
# 들어가야 한다 — "잘못된 값입니다" 만으로는 작성자가 무엇을 고쳐야 할지 알 수 없다.
# params 로 label/제약을 함께 실어 보내 클라가 해당 입력칸에 붙일 수 있게 한다.

ISSUE_FIELD_REQUIRED = REPORTS.code(
    "ISSUE_FIELD_REQUIRED",
    400,
    # {field} 는 항상 채워진다 — issue_fields.validate_and_normalize_values 가
    # label(없으면 id)을 반드시 넘긴다. 필드가 여러 개인 폼에서 이름이 없으면
    # 작성자는 어느 칸을 고쳐야 하는지 알 수 없다.
    '"{field}" is required.',
    hint="Fill it in and submit again.",
)

ISSUE_FIELD_VALUE_INVALID = REPORTS.code(
    "ISSUE_FIELD_VALUE_INVALID",
    400,
    '"{field}" has a value that is not allowed.',
    # 이유는 여러 가지(범위·정수·선택지·길이)라 호출부가 hint 로 구체화한다.
    hint="Check the field's allowed range or options and try again.",
)

ISSUE_FIELD_VALUES_MALFORMED = REPORTS.code(
    "ISSUE_FIELD_VALUES_MALFORMED",
    400,
    "The form answers are not in a readable format.",
    hint="Reload the form and enter the values again.",
)
