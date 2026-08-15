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
