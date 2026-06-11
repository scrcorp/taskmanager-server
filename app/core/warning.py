"""경고(Warning) 도메인 상수 — 사유 카테고리 기본값(시드).

현행 종이 양식(temp/warning_sample.pdf, "EMPLOYEE WARNING NOTICE FORM")의
Section 1 "reasons" 12개가 기본 카테고리. v1.1 부터 카테고리는 **org별 DB 테이블
(warning_categories)** 로 관리되며(추가/숨김/삭제), 아래 목록이 **org 생성 시 시드
기본값**이다 (라벨 포함 — 라벨도 DB로 내려감).

규칙:
    - `other` 는 system 카테고리(자유텍스트 사유 구동). 항상 맨 끝, 숨김/삭제 불가.
    - `refusal_overtime` 은 시드 시 hidden (사용자 요청 — 삭제 말고 숨김).
    - 코드 검증은 더 이상 이 frozenset 이 아니라 org 의 활성 카테고리로 한다
      (app.services.warning_category_service). frozenset 은 시드/하위호환용으로만 잔존.
"""

# 기본 카테고리 시드 — (code, label, is_hidden, is_system). 순서 = 양식 컬럼 순.
DEFAULT_WARNING_CATEGORIES: list[tuple[str, str, bool, bool]] = [
    ("tardiness", "Tardiness", False, False),
    ("damaged_equipment", "Damaged equipment", False, False),
    ("refusal_overtime", "Refusal to work overtime", True, False),  # 시드 시 숨김
    ("absenteeism", "Absenteeism", False, False),
    ("policy_violation", "Policy violation", False, False),
    ("insubordination", "Insubordination", False, False),
    ("rudeness", "Rudeness", False, False),
    ("fighting", "Fighting", False, False),
    ("language", "Language", False, False),
    ("failure_procedure", "Failure to follow procedure", False, False),
    ("failure_performance", "Failure to meet performance standards", False, False),
    ("other", "Other", False, True),  # system — 항상 맨 끝, 숨김/삭제 불가
]

# system 카테고리는 항상 맨 끝으로 정렬되도록 큰 sort_order 를 준다.
SYSTEM_CATEGORY_SORT = 9000

# 자유텍스트 사유를 구동하는 system 카테고리 코드 (숨김/삭제 불가).
SYSTEM_CATEGORY_CODE = "other"

# 하위호환 — 기본 코드 집합 (검증은 org 활성 카테고리로 이전됨, 시드/참조용으로만 잔존).
WARNING_CATEGORY_CODES: frozenset[str] = frozenset(c for c, *_ in DEFAULT_WARNING_CATEGORIES)

# 상태 값
#   active    — 유효한 경고 (정상 발행)
#   withdrawn — 철회됨 (잘못 발행해 거둬들임). 삭제와 달리 기록은 남는다 —
#               "누가 계속 잘못 발행하는지" 감사를 위해 목록에 표시된다.
WARNING_STATUS_ACTIVE = "active"
WARNING_STATUS_WITHDRAWN = "withdrawn"
WARNING_STATUSES: frozenset[str] = frozenset({WARNING_STATUS_ACTIVE, WARNING_STATUS_WITHDRAWN})
