"""Payroll L3 lock guard — confirmed pay period 내 날짜의 소급 변경 차단.

설계방향 L3 (2026-08-03 payroll-v1-설계방향.md):
    확정(confirm) 시 해당 기간의 근태 수정 잠금 + 소급 변경 전부 차단
    — lock 기간의 과거 날짜 스케줄 생성·수정·삭제 포함.
    수정이 필요하면 정정(amendment, L4) 경로 하나로만.

이 모듈이 **유일한 판정 지점**이다. attendance/schedule 도메인의 모든 mutation
게이트는 `ensure_not_locked()` 를 호출한다 — is_date_locked 를 직접 부르거나
자체 판정을 fork 하지 않는다 (게이트 간 판정 불일치 방지).

가드 범위 (호출부):
    - attendance: correct_attendance, state-machine 7액션, break 세션 CRUD
    - schedule: create/update/delete/confirm/revert/cancel/switch (+ bulk 경로,
      request 플로우의 create/update/delete/revert/confirm)
    - cron: auto clock-out (+ late/no_show 승격) — locked 날짜 row skip

가드하지 않는 것 (의도적):
    - 키오스크 실시간 clock 경로 — 오늘 날짜에만 동작, locked 날짜는 항상 과거
    - 자동퇴근 "확인"(L6, auto_clock_out_confirmed_*) — 검증 메타데이터일 뿐
      시간·금액 데이터가 아니므로 lock 후에도 기록 가능
    - correction reason 인라인 수정 — audit 메타데이터만 변경
"""

from __future__ import annotations

from datetime import date as DateType
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.payroll_period_service import payroll_period_service


# 콘솔/테스트가 매칭하는 고정 프리픽스 — 문구 변경 시 게이트 전체에 일괄 반영된다.
PAY_PERIOD_LOCKED_MESSAGE = "Pay period for this date is confirmed and locked"


class PayPeriodLockedError(HTTPException):
    """409 Conflict — 대상 날짜가 확정(confirmed)된 pay period 안일 때.

    detail 은 plain string (BadRequestError/DuplicateError 관례 일치) —
    bulk 경로의 errors 목록에도 그대로 읽히는 문장으로 들어간다.
    """

    def __init__(self, work_date: DateType | None = None) -> None:
        message = PAY_PERIOD_LOCKED_MESSAGE
        if work_date is not None:
            message += f" ({work_date.isoformat()})"
        message += (
            ". Records in a confirmed period cannot be changed here"
            " — use a payroll amendment to correct them."
        )
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=message)


async def ensure_not_locked(
    db: AsyncSession,
    *,
    store_id: Optional[UUID],
    work_date: Optional[DateType],
) -> None:
    """work_date 가 해당 매장의 confirmed pay period 안이면 409 로 거부.

    L3 lock enforcement 의 단일 게이트 헬퍼. mutation 직전에 호출한다.

    store_id / work_date 가 None 이면 판정 불가 → 통과.
    (store SET NULL 로 유실된 row 는 pay period 자체가 store 단위라 잠글 대상이
    없다 — confirm 시점의 entry 스냅샷은 이미 동결돼 있으므로 안전.)

    Raises:
        PayPeriodLockedError: 409 + "Pay period for this date is confirmed and locked"
    """
    if store_id is None or work_date is None:
        return
    if await payroll_period_service.is_date_locked(
        db, store_id=store_id, work_date=work_date
    ):
        raise PayPeriodLockedError(work_date)


async def is_locked_cached(
    db: AsyncSession,
    cache: dict[tuple[UUID, DateType], bool],
    *,
    store_id: Optional[UUID],
    work_date: Optional[DateType],
) -> bool:
    """루프(cron/bulk)용 저비용 판정 — (store_id, work_date) 별 1회만 조회.

    raise 하지 않고 bool 만 반환한다. 호출부가 skip / 에러 수집을 결정.
    """
    if store_id is None or work_date is None:
        return False
    key = (store_id, work_date)
    if key not in cache:
        cache[key] = await payroll_period_service.is_date_locked(
            db, store_id=store_id, work_date=work_date
        )
    return cache[key]
