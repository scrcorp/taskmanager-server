"""Payroll v1 계산 규칙 상수 — CA (Buena Park, Orange County) 기준.

계산 엔진(payroll calc)·penalty 감지(payroll_event_service)·마감 게이트가
공유하는 단일 상수 모듈 (스펙: docs/99_inbox/2026-08-03 payroll-v1-스키마-스펙.md
계산 규칙 확정 1~5, 설계방향 C2/C5/C8).

v1 정책:
    - 값은 전부 하드코딩 (CA 단일 매장 전제). 백오피스/설정화 이관은 v2 백로그.
    - penalty 감지 임계값(meal 6h[blanket waiver] / rest 3.5h 단계)은 v1 presence 기반 규칙 —
      타이밍 기반 고도화("5th hour 종료 전 meal 시작" 등)는 v2 (payroll_event_service 참조).
"""

from datetime import date
from decimal import Decimal


def payroll_period_label(start_date: date) -> str:
    """기간 시작일 → `YYYY.MM.FH|LH` — 회계사와 공유하는 급여기간 식별자.

    1–15일 시작이면 FH(first half), 16일 이후면 LH(late half).
    CFS 파일의 `payroll_id` 칸과 export 파일명이 같은 값을 쓰도록 여기 한 곳에 둔다.
    """
    half = "FH" if start_date.day <= 15 else "LH"
    return f"{start_date.year}.{start_date.month:02d}.{half}"

# ── C2: CA 일일/주간 OT·DT 임계 (단위: 시간) ─────────────────────────────
# 일 8h 초과 1.5x / 일 12h 초과 2x / 주(Sun–Sat, C3) 40h 초과 1.5x
CA_DAILY_OT_HOURS = 8
CA_DAILY_DT_HOURS = 12
WEEKLY_OT_HOURS = 40

# ── C8: 법정 최저임금 ────────────────────────────────────────────────────
# CA 법정 최저 $16.90 하드코딩 — 백오피스 이관 예정 (외부 Q6 값 검증 대기).
# default·개인 시급이 이 값 미만이면 경고 (floor 검증은 마감 게이트에서).
MINIMUM_WAGE = Decimal("16.90")

# ── C5: meal/rest penalty 부과량 ─────────────────────────────────────────
# 위반 시 1시간분 임금 가산 (카테고리별 일 1건 — day-grain unique 가 보장),
# 하루 최대 2시간분 (meal 1h + rest 1h).
PENALTY_HOURS = 1
DAY_PENALTY_MAX_HOURS = 2

# ── meal penalty 감지 임계 (CA Labor Code §512(a)) ───────────────────────
# 5h 초과 근무 시 30분 무급 식사시간 1회. 단 **총 근무가 6h 이하면 상호 합의로
# 면제(waiver) 가능** — 이 면제를 빼먹으면 5~6h 근무를 전부 위반으로 잡는다
# (실측: 07.LH 재현에서 meal penalty 1,261건 중 833건이 이 구간이었다).
# 10h 초과 시 2회차 식사시간. 총 12h 이하이고 1회차를 실제로 썼으면 2회차 면제 가능.
MEAL_PENALTY_NET_MINUTES = 5 * 60  # 300 — 의무 발생 기준
MEAL_WAIVER_MAX_MINUTES = 6 * 60  # 360 — 여기까지는 면제 가능 (1회차)
SECOND_MEAL_NET_MINUTES = 10 * 60  # 600 — 2회차 의무 발생
SECOND_MEAL_WAIVER_MAX_MINUTES = 12 * 60  # 720 — 여기까지는 2회차 면제 가능
MEAL_BREAK_MIN_MINUTES = 30


def required_meal_breaks(net_minutes: int) -> int:
    """일 net 근무시간(분) → 필요한 30분 무급 식사시간 횟수 (§512(a)).

    면제(waiver)는 서면 합의가 전제다. 우리는 합의가 있다고 보고 면제 구간을
    위반으로 잡지 않는다 — 합의서가 없는 매장이라면 이 함수가 아니라
    매장 설정으로 꺼야 한다 (v2).

    단계:
        ≤ 5h        → 0 (의무 없음)
        5h 초과 ~ 6h → 0 (면제 가능)
        6h 초과 ~ 12h → 1
        12h 초과     → 2

    10~12h 구간은 법문상 "2회차를 면제 가능"이라 1회로 충분하다. 다만 그 면제는
    1회차를 실제로 썼을 때만 유효한데, 우리 판정은 '몇 개를 썼는가'로 하므로
    1개도 안 썼으면 required=1 에 걸려 그대로 위반이 된다 — 결과가 같다.
    """
    if net_minutes <= MEAL_WAIVER_MAX_MINUTES:
        return 0
    if net_minutes <= SECOND_MEAL_WAIVER_MAX_MINUTES:
        return 1
    return 2

# ── rest penalty 감지 임계 (v1 presence 기반) ────────────────────────────
# 일 C1 net ≥ 3.5h 부터 유급 10분 휴게 의무 발생. 필요 세션 수는
# required_rest_breaks() 단계 함수 참조.
REST_PENALTY_MIN_NET_MINUTES = 210  # 3.5h
_REST_TIER_2_MINUTES = 6 * 60   # 6h 초과부터 2회
_REST_TIER_3_MINUTES = 10 * 60  # 10h 초과부터 3회
_REST_TIER_4_MINUTES = 14 * 60  # 14h 초과부터 4회


def required_rest_breaks(net_minutes: int) -> int:
    """일 net 근무시간(분) → 필요한 유급 10분 휴게 세션 수 (v1 규칙).

    단계: < 3.5h → 0 / 3.5–6h → 1 / >6–10h → 2 / >10–14h → 3 / >14h → 4
    (4시간마다 1회 — "or major fraction thereof")
    """
    if net_minutes < REST_PENALTY_MIN_NET_MINUTES:
        return 0
    if net_minutes <= _REST_TIER_2_MINUTES:
        return 1
    if net_minutes <= _REST_TIER_3_MINUTES:
        return 2
    if net_minutes <= _REST_TIER_4_MINUTES:
        return 3
    return 4


# ── payroll_events.kind (스펙 §6) ────────────────────────────────────────
EVENT_KIND_MEAL_PENALTY = "meal_penalty"
EVENT_KIND_REST_PENALTY = "rest_penalty"
EVENT_KIND_DAILY_OT = "daily_ot"
EVENT_KIND_WEEKLY_OT = "weekly_ot"
EVENT_KIND_SEVENTH_DAY = "seventh_day"

# 감지 소스별 kind 그룹 — penalty 는 attendance 재감지, classification 은 계산 엔진 출력.
PENALTY_EVENT_KINDS = frozenset({EVENT_KIND_MEAL_PENALTY, EVENT_KIND_REST_PENALTY})
CLASSIFICATION_EVENT_KINDS = frozenset(
    {EVENT_KIND_DAILY_OT, EVENT_KIND_WEEKLY_OT, EVENT_KIND_SEVENTH_DAY}
)
VALID_EVENT_KINDS = PENALTY_EVENT_KINDS | CLASSIFICATION_EVENT_KINDS

# ── E1: 귀책 태그 ────────────────────────────────────────────────────────
ATTRIBUTION_STAFF = "staff"
ATTRIBUTION_MANAGEMENT = "management"
VALID_ATTRIBUTIONS = frozenset({ATTRIBUTION_STAFF, ATTRIBUTION_MANAGEMENT})
