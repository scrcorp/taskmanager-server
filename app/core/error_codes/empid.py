"""EMPID 채번 — 커서 조정·재계산·번호대 문맥.

계약 SoT: `docs/99_inbox/2026-08-18 empid 채번 API계약·규칙.md` §4.
계약 문서는 코드를 `ERR-REASON-REQUIRED` 처럼 대시로 적었지만 레지스트리는
`UPPER_SNAKE` 만 허용한다(E3) — 대시를 밑줄로 바꾼 1:1 대응이다.

문구는 계약 §4 표 그대로 쓴다(원인 + 다음 행동, 영문 UI).
"""

from __future__ import annotations

from app.core.error_codes._registry import domain

EMPID = domain("empid")

ERR_REASON_REQUIRED = EMPID.code(
    "ERR_REASON_REQUIRED",
    422,
    "Enter a reason for this change.",
)

ERR_CURSOR_INVALID = EMPID.code(
    "ERR_CURSOR_INVALID",
    422,
    "Next EMPID must be a whole number of 1 or more.",
)

# 매장이 Shared 그룹(numbering_mode="group")에 속하면 번호대·커서는 그룹이 갖는다.
# 예전에는 매장 number_range_start 를 조용히 저장하고 채번에서 무시했다 — 조용한 실패.
ERR_RANGE_IGNORED = EMPID.code(
    "ERR_RANGE_IGNORED",
    422,
    "This store follows its group's shared numbering. Change it in Groups.",
)
