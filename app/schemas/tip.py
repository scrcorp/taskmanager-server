"""팁 관련 Pydantic 요청/응답 스키마.

Tip-related Pydantic request/response schemas.

Covers:
    - TipEntry create/update/response (직원 일별 입력)
    - TipDistribution create/response (동료 분배)
    - 검증: 분배 합 ≤ card_tips, 금액 ≥ 0
"""

from __future__ import annotations

from datetime import date as DateType, datetime
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


# ── Distribution ────────────────────────────────────────────────

class TipDistributionCreate(BaseModel):
    """분배 입력 — entry create 시 nested 또는 entry update 시 사용."""

    receiver_id: UUID
    amount: Decimal = Field(ge=Decimal("0"))
    reason: Optional[str] = None


class TipDistributionResponse(BaseModel):
    """분배 응답."""

    id: UUID
    entry_id: UUID
    receiver_id: Optional[UUID]
    receiver_name: Optional[str]
    amount: Decimal
    reason: Optional[str]
    status: Literal["pending", "accepted", "auto_accepted"]
    pending_until: datetime
    accepted_at: Optional[datetime]
    created_at: datetime


class TipDistributionIncomingResponse(BaseModel):
    """본인이 받은 분배 — sender 정보 포함."""

    id: UUID
    entry_id: UUID
    sender_id: UUID
    sender_name: str
    sender_store_id: UUID
    sender_store_name: Optional[str]
    work_role_name: Optional[str]
    work_date: DateType
    amount: Decimal
    reason: Optional[str]
    status: Literal["pending", "accepted", "auto_accepted"]
    pending_until: datetime
    accepted_at: Optional[datetime]
    created_at: datetime


# ── Entry ────────────────────────────────────────────────────────

class TipEntryCreate(BaseModel):
    """직원 entry 생성 — schedule 기반.

    schedule_id 가 필수 (한 schedule 당 1건). store/work_role/date 는 서버에서
    schedule 으로부터 derive 한다.
    source: attendance / staff_app — manager API 는 별도 스키마.
    """

    schedule_id: UUID
    card_tips: Decimal = Field(ge=Decimal("0"))
    cash_tips_kept: Decimal = Field(ge=Decimal("0"))
    source: Literal["attendance", "staff_app"] = "staff_app"
    distributions: list[TipDistributionCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_distribution_total(self) -> "TipEntryCreate":
        total = sum((d.amount for d in self.distributions), Decimal("0"))
        if total > self.card_tips:
            raise ValueError(
                f"Distributed exceeds card tips by ${total - self.card_tips:.2f}"
            )
        return self


class TipEntryUpdate(BaseModel):
    """직원 entry 수정 — 분배는 통째로 교체 (단순화).

    수정 시 distributions 가 들어오면 기존을 모두 삭제하고 새로 생성.
    None 이면 분배는 그대로 둠.
    """

    card_tips: Optional[Decimal] = Field(default=None, ge=Decimal("0"))
    cash_tips_kept: Optional[Decimal] = Field(default=None, ge=Decimal("0"))
    distributions: Optional[list[TipDistributionCreate]] = None


class ManagerTipEntryCreate(BaseModel):
    """매니저가 직원 대신 entry 추가 — comment 필수.

    schedule_id 를 주면 schedule 기반 (store/work_role/date 자동 derive).
    schedule_id 없이 freeform 입력도 허용 (store_id + date 필수, work_role 옵션).
    """

    employee_id: UUID
    schedule_id: Optional[UUID] = None
    # freeform 입력 시 (schedule_id 없을 때) 필수.
    store_id: Optional[UUID] = None
    work_role_id: Optional[UUID] = None
    date: Optional[DateType] = None
    card_tips: Decimal = Field(ge=Decimal("0"))
    cash_tips_kept: Decimal = Field(ge=Decimal("0"))
    comment: str = Field(min_length=1)
    distributions: list[TipDistributionCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_consistency(self) -> "ManagerTipEntryCreate":
        # schedule_id 없으면 freeform 필드 필수
        if self.schedule_id is None and (self.store_id is None or self.date is None):
            raise ValueError(
                "Either schedule_id or (store_id + date) is required"
            )
        total = sum((d.amount for d in self.distributions), Decimal("0"))
        if total > self.card_tips:
            raise ValueError(
                f"Distributed exceeds card tips by ${total - self.card_tips:.2f}"
            )
        return self


class ManagerTipEntryUpdate(BaseModel):
    """매니저 수정 — comment 필수. distributions 가 None 이면 분배 그대로 둠."""

    card_tips: Optional[Decimal] = Field(default=None, ge=Decimal("0"))
    cash_tips_kept: Optional[Decimal] = Field(default=None, ge=Decimal("0"))
    comment: str = Field(min_length=1)
    distributions: Optional[list[TipDistributionCreate]] = None


class PeriodConfirmRequest(BaseModel):
    """일반 confirm (정상 종료) — 추가 정보 없음."""

    pass


class PeriodForceCloseRequest(BaseModel):
    """Force-close — reason 10자+ 필수."""

    reason: str = Field(min_length=10)


class PeriodKPI(BaseModel):
    card_total: Decimal
    cash_total: Decimal
    distributed_total: Decimal
    reported_total: Decimal
    entries_count: int
    distinct_employees: int


class PeriodDailyRow(BaseModel):
    date: DateType
    reported: Decimal


class PeriodEmployeeRow(BaseModel):
    employee_id: UUID
    employee_name: str
    card: Decimal
    cash: Decimal
    distributed: Decimal
    reported: Decimal
    entries: int


class PeriodDashboardResponse(BaseModel):
    store_id: UUID
    start_date: DateType
    end_date: DateType
    status: Literal["open", "confirmed"]
    confirmed_at: Optional[datetime]
    confirmed_by: Optional[UUID]
    override_reason: Optional[str]
    kpi: PeriodKPI
    daily: list[PeriodDailyRow]
    per_employee: list[PeriodEmployeeRow]


class AuditLogResponse(BaseModel):
    id: UUID
    entity_type: str
    entity_id: UUID
    action: str
    actor_id: Optional[UUID]
    actor_name: Optional[str]
    comment: Optional[str]
    before: Optional[dict]
    after: Optional[dict]
    created_at: datetime


class StoreDistributionResponse(BaseModel):
    """매장 단위 분배 응답 (Distributions 탭)."""

    id: UUID
    entry_id: UUID
    sender_id: UUID
    sender_name: str
    receiver_id: Optional[UUID]
    receiver_name: Optional[str]
    work_role_name: Optional[str]
    work_date: DateType
    amount: Decimal
    reason: Optional[str]
    status: Literal["pending", "accepted", "auto_accepted"]
    pending_until: datetime
    accepted_at: Optional[datetime]
    created_at: datetime


class Form4070Response(BaseModel):
    id: UUID
    employee_id: UUID
    employee_name: Optional[str] = None
    period_id: UUID
    period_start: DateType
    period_end: DateType
    store_id: UUID
    store_name: Optional[str] = None
    pdf_key: Optional[str]
    pdf_url: Optional[str] = None
    reported_cash: Decimal
    reported_card: Decimal
    paid_out: Decimal
    net_tips: Decimal
    status: Literal["generated", "downloaded", "signed", "unsigned"]
    generated_at: datetime
    signed_at: Optional[datetime]
    signature_image_key: Optional[str]
    signature_url: Optional[str] = None


class SignFormRequest(BaseModel):
    signature_image_key: str
    save_for_future: bool = False


class SignatureUpdateRequest(BaseModel):
    signature_image_key: str


class SignatureResponse(BaseModel):
    signature_image_key: Optional[str]
    signature_url: Optional[str] = None


class TipEntryResponse(BaseModel):
    """Entry 응답 — 분배 nested 포함 + 계산값."""

    id: UUID
    schedule_id: Optional[UUID]
    schedule_start_time: Optional[str] = None
    schedule_end_time: Optional[str] = None
    store_id: UUID
    store_name: Optional[str]
    employee_id: UUID
    work_role_id: Optional[UUID]
    work_role_name: Optional[str]
    date: DateType
    card_tips: Decimal
    cash_tips_kept: Decimal
    source: str
    last_modified_by_id: Optional[UUID]
    last_modified_at: Optional[datetime]
    last_manager_note: Optional[str] = None
    last_modified_by_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    distributions: list[TipDistributionResponse]
    # 계산값
    distributed_total: Decimal
    reportable_card: Decimal
    reported_on_4070: Decimal
