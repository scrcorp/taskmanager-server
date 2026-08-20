"""Payroll breakdown/preview Pydantic 계약 — Payroll v1 Phase 2 (calc_version=1).

payroll_entries.breakdown(JSONB) 의 **고정 계약** (스펙 §5):
    - EntryBreakdown 이 유일한 포맷 정의. confirm 시 이 모델로 직렬화해 동결하고,
      동결된 과거 entry 는 calc_version 으로 렌더 호환을 보장한다.
    - 스칼라 컬럼(regular_pay 등)은 breakdown 합계이며 confirm 시 일치 검증
      (라운딩: 구간(segment)별 계산 후 합산 — 센트 반올림은 구간 레벨 1회,
       합계는 이미 반올림된 구간 금액의 정확한 합).
    - **선택 필드 추가는 calc_version 을 올리지 않는다** (additive-compatible):
      옛 동결본은 새 필드가 None 으로 파싱되면 그만이고, 재계산하지 않는다.
      기존 필드의 의미/타입이 바뀔 때만 버전을 올린다.

preview(미리보기) 응답 모델(PayrollPreviewRow/PreviewValidation)도 여기 —
계산 엔진(payroll_calc_service)이 조립하고 Phase 3 콘솔 API 가 그대로 노출한다.

돈은 전부 Decimal (Numeric(10,2) 관례). 분(minutes)은 정수 유지 (C7 절사 유지).
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

# breakdown 포맷 버전 — 포맷이 바뀔 때만 올린다 (동결 entry 렌더 보장).
CALC_VERSION = 1

# ── preview validation codes (마감 게이트 L5 / 계산 규칙 4·5 의 사전 경고) ──
VALIDATION_RATE_MISSING = "rate_missing"  # rate_at 결과 None 또는 ≤ 0 (계산 규칙 5)
VALIDATION_BELOW_MINIMUM_WAGE = "below_minimum_wage"  # 적용 rate < 법정 최저 (C8)
VALIDATION_OPEN_SHIFT = "open_shift"  # clock_out 없는 열린 근무 (L5-②)
# 스케줄은 있었는데 clock-in 이 없어 cron 이 no_show 로 승격한 근무 — "진짜 결근"과
# "일했는데 기록 안 됨"을 구분할 수 없어 확정 전 사람이 본다. 경고만 (차단 게이트 아님).
VALIDATION_NO_SHOW = "no_show"
# 자동퇴근 미확인 — Phase 3 에서 auto_clock_out_confirmed_* 컬럼으로 정교화 예정 (L5-①)
VALIDATION_UNCONFIRMED_AUTO_CLOCKOUT = "unconfirmed_auto_clockout"
VALIDATION_TIP_PERIOD_NOT_CONFIRMED = "tip_period_not_confirmed"  # 계산 규칙 4
# 조기 출근 강행 미확인 — 예정 밖 근무시간이 급여에 그대로 들어가므로 사람이 한 번 본다
VALIDATION_UNCONFIRMED_EARLY_CLOCK_IN = "unconfirmed_early_clock_in"
# 겹친 근무 — 같은 사람의 두 근무가 시간상 겹치면 같은 시간이 두 번 지급된다 (D15).
# **확인 도장(escape hatch)이 없다** — "승인된 겹침" 은 정의상 이중 지급이라
# 해소 경로는 한쪽을 정정/취소하는 것뿐이다.
VALIDATION_OVERLAPPING_ATTENDANCE = "overlapping_attendance"


class RateSegment(BaseModel):
    """rate 구간 1개 — 같은 적용 시급을 받은 일들의 합계 (명세서 rate별 표기, E4/R4).

    amount 는 이 구간에 귀속된 총액. OT/DT premium 의 base 는 주 단위로 결정되므로
    (멀티 rate 주 = 가중평균, 계산 규칙 1) rate × 분으로 단순 역산되지 않을 수 있다
    — 일별 상세는 days, 주 단위 base 는 계산 로그가 원천.
    """

    rate: Decimal  # 이 구간의 적용 시급 (rate 미상 일들은 0)
    regular_minutes: int = 0
    ot_minutes: int = 0  # 1.5x 분 (daily + weekly + 7th-day ≤8h 합산)
    dt_minutes: int = 0  # 2x 분 (daily >12h + 7th-day >8h 합산)
    amount: Decimal  # 구간 귀속 총액 (reg/ot/dt 각각 센트 반올림 후 합)


class WorkedShift(BaseModel):
    """그날 근무 1건의 벽시계 구간 — 매장 타임존 기준 "HH:MM" (C7 분 계산과 무관).

    자정을 넘겨도 시:분만 담는다 (22:00~06:30). 날짜는 소속 DayDetail 이 갖고
    있고, 지급 판정은 이미 끝난 값이라 여기서 다시 계산할 것이 없다.
    """

    start: str  # "08:00"
    end: str | None = None  # 미퇴근이면 None


class WorkedBreak(BaseModel):
    """그날 휴게 1건 — 매장 타임존 벽시계 + 종류 (유/무급 구분 표시용)."""

    start: str
    end: str | None = None
    type: str  # paid_10min | unpaid_meal


class DayDetail(BaseModel):
    """일별 분류 상세 — 지급 대상 일(현 기간 내)만 담는다.

    ot/dt 는 병합 최종값 (일별·주간·7일 규칙 중 시간당 높은 분류, C2).

    금액 필드(*_amount)는 **표시 전용 정보값**이고 additive-compatible 추가다
    (calc_version=1 유지 — 포맷이 깨지지 않는 선택 필드):
        - 이 필드가 없던 시절 동결된 breakdown 은 None 으로 파싱된다. 과거
          entry 를 재계산하지 않으며, 표시 쪽이 None 을 다뤄야 한다.
        - canonical 금액은 여전히 segments + 스칼라 컬럼이다. 일별 금액은
          하루 단위로 센트 반올림하므로 일별 합이 segment 합과 센트 단위로
          어긋날 수 있다 (confirm 일치 검증은 segment 기준 — 일별 금액은
          검증 대상이 아니다).
    """

    work_date: date
    regular_minutes: int = 0
    ot_minutes: int = 0
    dt_minutes: int = 0
    applied_rate: Decimal | None = None  # rate_at(그날). None = rate 미상 (게이트 대상)
    # 그날 applied_rate 기준 정규 금액
    regular_amount: Decimal | None = None
    # 1.5x / 2x — base 는 그 주의 OT base (멀티 rate 주면 분-가중평균, 계산 규칙 1)
    ot_amount: Decimal | None = None
    dt_amount: Decimal | None = None
    # 위 3개의 합 = 그날 근무로 받는 금액 (penalty/카드팁 제외 — 각각 별도 라인)
    total_amount: Decimal | None = None
    # 그날 실제 근무/휴게 시각 (store-tz 벽시계) — 금액과 같은 additive 선택 필드.
    # 옛 동결본과 전기 frozen 소스 일자는 빈 목록 (재계산하지 않는다).
    shifts: list[WorkedShift] = Field(default_factory=list)
    breaks: list[WorkedBreak] = Field(default_factory=list)
    # 그날 대표 매장 (net 최다) — group 스코프에선 기간에 매장이 없어서
    # 이 값이 없으면 콘솔이 "어느 매장 근태인지" 를 알 방법이 없다 (근태 딥링크가
    # 기본 매장으로 열리는 회귀의 원인). additive 선택 필드 — 옛 동결본은 None.
    store_id: str | None = None
    # 그날 근무한 매장 전체 (같은 날 그룹 내 두 매장이면 2개). 표시/링크 판단용.
    store_ids: list[str] = Field(default_factory=list)
    # 그날 attendance 가 정확히 1건일 때만 — 콘솔이 목록 대신 상세로 바로 연다.
    # split shift·같은 날 두 매장이면 None (하나로 특정할 수 없다).
    attendance_id: str | None = None


class PenaltyLine(BaseModel):
    """meal/rest penalty 1건 — 사유와 금액 (C5: 명세서/마감 화면 노출)."""

    work_date: date
    kind: str  # meal_penalty | rest_penalty
    reason: str  # payroll_events.reason 스냅샷 (사람이 읽는 사유)
    amount: Decimal  # 1h × 그날 rate, 일 상한 2h 클램프 후 (payroll_rules)



class ContextDay(BaseModel):
    """직전 기간 일자 1건 — 이 기간의 주 판정에는 들어갔지만 지급은 전 기간 몫.

    경계 걸친 주(계산 규칙 3/C4)에서 주 40h·7일 연속 판정은 그 주 전체를 보고,
    지급은 현 기간 일자만 한다. 그래서 "이 주는 이미 40h였다"의 근거가 breakdown
    안에 남지 않았다 — 이 목록이 그 근거다.

    분류(reg/ot/dt)는 담지 않는다: 그 일자의 분류·금액은 **전 기간 명세서**가
    원천이고, 여기서 재서술하면 두 기간이 서로 다른 값을 주장할 수 있다.
    """

    work_date: date
    net_minutes: int = 0  # 그날 C1 net 분 (주 판정에 합산된 양)
    paid_in_prior: bool = True  # 지급은 직전 기간에서 — 이 기간 총계에 미포함


class BonusLine(BaseModel):
    """성과 보너스 1건 — 매장 단위 (group 스코프 전환 후 다중 매장 지원).

    보너스 가산율은 (member, store) 값이라 그룹 기간에서는 매장별로 갈릴 수 있다.
    지급액 = rate × 그 매장 근무 분/60 (OT 할증 없음). additive 선택 필드 —
    옛 동결본은 빈 목록으로 파싱되고, 그 경우 bonus_rate 스칼라가 원천이다.
    """

    store_id: str  # 매장 UUID 문자열
    rate: Decimal  # 시급 가산율
    minutes: int  # 그 매장 근무 분 (기간 내 지급 대상 분)
    amount: Decimal  # rate × minutes / 60, 센트 반올림


class EntryBreakdown(BaseModel):
    """payroll_entries.breakdown 전체 — calc_version=1 계약 (스펙 §5)."""

    calc_version: int = CALC_VERSION
    segments: list[RateSegment] = Field(default_factory=list)  # rate 오름차순
    days: list[DayDetail] = Field(default_factory=list)  # work_date 오름차순
    penalties: list[PenaltyLine] = Field(default_factory=list)  # (date, kind) 오름차순
    # 경계 걸친 주의 직전 기간 일자 (지급 아님 — 판정 근거). work_date 오름차순.
    # 선택 필드 추가라 calc_version 유지 — 옛 동결본은 빈 목록으로 파싱된다.
    context_days: list[ContextDay] = Field(default_factory=list)
    # 성과 보너스 가산율 (시급 add-on). 지급액은 bonus_rate × 총시간 — OT 할증 없음.
    # 선택 필드 추가라 calc_version 유지 (옛 동결본은 0 으로 파싱).
    bonus_rate: Decimal = Decimal("0.00")
    # 매장별 보너스 라인 (group 스코프) — 있으면 bonus_pay 검증의 원천.
    # additive 선택 필드 (옛 동결본은 빈 목록 → bonus_rate × 총시간 공식으로 검증).
    bonus_lines: list[BonusLine] = Field(default_factory=list)
    tip_period_id: str | None = None  # 대응 tip_period UUID (단일 매장일 때만, 하위호환)
    # group 스코프: 그룹 내 매장별 대응 tip_period UUID 목록 (계산 규칙 4 추적).
    # additive 선택 필드 — 옛 동결본은 빈 목록.
    tip_period_ids: list[str] = Field(default_factory=list)
    # 계산 입력 출처 메모 (선택) — 예: 경계 걸친 주의 전기 frozen 참조 (계산 규칙 3)
    sources: dict | None = None


class PreviewValidation(BaseModel):
    """preview 경고 1건 — 마감 게이트(L5)의 사전 표시용 (차단은 confirm 쪽)."""

    code: str  # VALIDATION_* 상수
    message: str  # 사람이 읽는 설명 (영어 — UI 텍스트 규칙)
    user_id: UUID | None = None  # 해당 직원 (기간 수준 경고면 None 가능)


class PayrollPreviewRow(BaseModel):
    """직원 1명의 기간 미리보기 — confirm 시 payroll_entries 로 동결될 값과 동일."""

    user_id: UUID
    member_name: str  # display_name() 경유 (스펙 E5)
    empid: int | None = None  # org_member_stores(entry 의 store 기준) 스냅샷
    crewid: int | None = None  # org_members.crewid 스냅샷
    regular_minutes: int = 0
    ot_minutes: int = 0
    dt_minutes: int = 0
    regular_pay: Decimal = Decimal("0.00")
    ot_pay: Decimal = Decimal("0.00")
    dt_pay: Decimal = Decimal("0.00")
    penalty_pay: Decimal = Decimal("0.00")
    bonus_pay: Decimal = Decimal("0.00")  # bonus_rate × 총시간 (OT 할증 없음)
    card_tips: Decimal = Decimal("0.00")  # C6: own_card − paid_out + received(수락)
    gross_pay: Decimal = Decimal("0.00")  # 위 6개 pay 의 합
    breakdown: EntryBreakdown
    validations: list[PreviewValidation] = Field(default_factory=list)


# ───────────────────────────────────────────────────────────────────────────
# Phase 3 — 콘솔 API 계약 (periods / confirm / entries / events)
# ───────────────────────────────────────────────────────────────────────────

# 마감 게이트(L5) 코드 — preview validation 과 같은 코드를 재사용하고,
# confirm 전용 게이트(멀티스토어 주간 정합, 계산 규칙 2)만 추가한다.
GATE_MULTI_STORE_WEEK = "multi_store_week"

# confirm 409 detail.code — 콘솔이 분기 렌더하는 구조화 에러 코드
CODE_CLOSE_GATES_FAILED = "payroll_close_gates_failed"
CODE_ALREADY_CONFIRMED = "pay_period_already_confirmed"


class PayPeriodResponse(BaseModel):
    """pay period 1건 — 목록/미리보기/확정 응답 공용."""

    id: UUID
    organization_id: UUID
    # 급여 스코프 = 법인 (2026-08-19 전환). 레거시(전환 전 확정) 기간만 NULL.
    store_group_id: UUID | None = None
    # 레거시 store 스코프 기간 전용 — 신규(group) 기간은 NULL.
    store_id: UUID | None = None
    start_date: date
    end_date: date
    status: str  # 'open' | 'confirmed'
    confirmed_at: datetime | None = None
    confirmed_by: UUID | None = None
    override_reason: str | None = None
    # 대응 tip_period status 요약 — 그룹 내 전 매장 confirmed 여야 'confirmed'.
    # None = 미생성 매장이 있음. 마감 게이트 ④ 의 사전 표시용 (계산 규칙 4).
    tip_period_status: str | None = None


class ValidationSummaryItem(BaseModel):
    """preview 응답의 validation 요약 1건 — code 별 발생 건수."""

    code: str  # VALIDATION_* 상수
    count: int  # 발생 건수 (행×코드 단위)


class PeriodPreviewResponse(BaseModel):
    """GET /payroll/periods/{id}/preview 응답."""

    period: PayPeriodResponse
    rows: list[PayrollPreviewRow] = Field(default_factory=list)
    validations_summary: list[ValidationSummaryItem] = Field(default_factory=list)


class PayrollEntryResponse(BaseModel):
    """동결된 payroll entry 1건 (confirm 응답 + entries 조회 공용)."""

    id: UUID
    pay_period_id: UUID
    user_id: UUID | None = None
    org_member_id: UUID | None = None
    empid: int | None = None
    crewid: int | None = None
    member_name: str
    revision: int = 0
    regular_minutes: int = 0
    ot_minutes: int = 0
    dt_minutes: int = 0
    regular_pay: Decimal = Decimal("0.00")
    ot_pay: Decimal = Decimal("0.00")
    dt_pay: Decimal = Decimal("0.00")
    penalty_pay: Decimal = Decimal("0.00")
    card_tips: Decimal = Decimal("0.00")
    gross_pay: Decimal = Decimal("0.00")
    calc_version: int
    breakdown: EntryBreakdown
    created_at: datetime


class PeriodConfirmResponse(BaseModel):
    """POST /payroll/periods/{id}/confirm 성공 응답 — 동결 결과 요약."""

    period: PayPeriodResponse
    entries: list[PayrollEntryResponse] = Field(default_factory=list)
    events_frozen: int = 0  # pay_period_id 가 찍힌(동결된) 이벤트 수


class PeriodEntriesResponse(BaseModel):
    """GET /payroll/periods/{id}/entries 응답 (confirm 전이면 entries=[])."""

    period: PayPeriodResponse
    entries: list[PayrollEntryResponse] = Field(default_factory=list)


class ConfirmGateItem(BaseModel):
    """게이트 실패 상세 1건 — 어느 직원의 어느 날짜가 문제인지."""

    user_id: UUID | None = None
    member_name: str | None = None
    dates: list[date] = Field(default_factory=list)
    message: str  # 사람이 읽는 상세 (영어 — UI 텍스트 규칙)


class ConfirmGateFailure(BaseModel):
    """마감 게이트 1개의 실패 묶음 — 409 detail.gates[] 요소."""

    gate: str  # VALIDATION_* 또는 GATE_MULTI_STORE_WEEK
    message: str  # 게이트 수준 설명 + 다음 행동 (actionable)
    items: list[ConfirmGateItem] = Field(default_factory=list)


class PayrollEventResponse(BaseModel):
    """payroll event 1건 — 금액 없는(운영 태깅용) 페이로드."""

    id: UUID
    user_id: UUID | None = None
    member_name: str | None = None  # display_name() — 계정 유실 시 None
    attendance_id: UUID | None = None
    work_date: date
    kind: str
    reason: str
    attribution: str | None = None  # staff | management | None(미태깅)
    tagged_by: UUID | None = None
    tagged_at: datetime | None = None
    voided_at: datetime | None = None
    pay_period_id: UUID | None = None
    frozen: bool = False  # pay_period_id 부여 여부 (동결 — 태깅 불가)
    created_at: datetime


class EventTagRequest(BaseModel):
    """POST /payroll/events/{id}/tag 본문 — 귀책 태그 (E1)."""

    # 의도적으로 Literal 미사용 — 잘못된 값은 서비스가 400 으로 응답 (422 아님)
    attribution: str


class DraftPayStubResponse(BaseModel):
    """미확정 기간 draft stub 생성 메타 (E4).

    동결 entry 가 없으므로 entry_id/file_id 가 없다 — draft 는 저장하지 않고
    요청마다 preview 로 다시 만든다 (확정 전 숫자를 파일로 박제하지 않는다).
    바이트는 같은 경로의 GET 이 서빙한다.
    """

    pay_period_id: UUID
    user_id: UUID
    filename: str  # PayStub_{이름}_{기간}_DRAFT.pdf
    size_bytes: int | None = None
    generated_at: datetime
    draft: bool = True  # 확정본 응답과 구분되는 표식


class PayStubResponse(BaseModel):
    """pay stub 생성/조회 메타 응답 (E4).

    파일 경로/URL 은 의도적으로 미노출 — 다운로드는 인증된 GET .../stub 이
    바이트를 직접 서빙한다 (warnings PDF 와 동일한 IDOR 방지 패턴).
    """

    entry_id: UUID
    pay_period_id: UUID
    file_id: UUID
    filename: str
    size_bytes: int | None = None
    generated_at: datetime


