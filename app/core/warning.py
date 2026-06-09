"""경고(Warning) 도메인 상수 — 사유 카테고리 코드.

현행 종이 양식(temp/warning_sample.pdf, "EMPLOYEE WARNING NOTICE FORM")의
Section 1 "reasons" 12개를 그대로 코드화한다. 추후 PDF 출력 시 1:1 매핑.

v1 은 코드 고정(아래 set). 추후 org 설정으로 가변화할 수 있으나 그때도 이 목록이
시드 기본값이 된다. 라벨(영문 표시)은 콘솔 프론트가 보유 — 백엔드는 코드만 검증.
"""

# 종이 양식의 12개 사유 코드 (multi-select). 순서는 양식 컬럼 순.
WARNING_CATEGORY_CODES: frozenset[str] = frozenset(
    {
        "tardiness",
        "damaged_equipment",
        "refusal_overtime",
        "absenteeism",
        "policy_violation",
        "insubordination",
        "rudeness",
        "fighting",
        "language",
        "failure_procedure",
        "failure_performance",
        "other",
    }
)

# 상태 값
#   active    — 유효한 경고 (정상 발행)
#   withdrawn — 철회됨 (잘못 발행해 거둬들임). 삭제와 달리 기록은 남는다 —
#               "누가 계속 잘못 발행하는지" 감사를 위해 목록에 표시된다.
WARNING_STATUS_ACTIVE = "active"
WARNING_STATUS_WITHDRAWN = "withdrawn"
WARNING_STATUSES: frozenset[str] = frozenset({WARNING_STATUS_ACTIVE, WARNING_STATUS_WITHDRAWN})
