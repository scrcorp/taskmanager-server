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
