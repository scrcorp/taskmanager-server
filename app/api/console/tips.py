"""매니저용 팁 API (Stage A — Review / Distributions 탭).

엔드포인트:
    GET   /api/v1/console/tips/entries?store_id=&start=&end=&employee_id=
    POST  /api/v1/console/tips/entries                (매니저 누락 추가, comment 필수)
    PATCH /api/v1/console/tips/entries/{entry_id}     (매니저 수정, comment 필수)
    GET   /api/v1/console/tips/distributions?store_id=&status=
"""

from __future__ import annotations

from datetime import date as DateType
from typing import Annotated, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, require_permission
from app.database import get_db
from app.models.organization import Store
from app.models.user import User
from app.schemas.tip import (
    AuditLogResponse,
    Form4070Response,
    ManagerTipEntryCreate,
    ManagerTipEntryUpdate,
    PeriodConfirmRequest,
    PeriodDashboardResponse,
    PeriodForceCloseRequest,
    StoreDistributionResponse,
    TipEntryResponse,
)
from app.services.tip_service import tip_service


router: APIRouter = APIRouter()


@router.get("/entries", response_model=list[TipEntryResponse])
async def list_store_entries(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:read"))],
    store_id: Annotated[UUID, Query()],
    start: Annotated[DateType, Query()],
    end: Annotated[DateType, Query()],
    employee_id: Annotated[Optional[UUID], Query()] = None,
) -> list[dict]:
    """매장 단위 entries (Review matrix 데이터)."""
    await check_store_access(db, current_user, store_id)
    entries = await tip_service.list_entries_for_store(
        db, store_id=store_id, start=start, end=end, employee_id=employee_id,
    )
    store_name = await db.scalar(select(Store.name).where(Store.id == store_id))
    return [
        tip_service.build_entry_response(
            e,
            store_name=store_name,
            schedule=getattr(e, "_schedule_loaded", None),
        )
        for e in entries
    ]


@router.post("/entries", response_model=TipEntryResponse, status_code=status.HTTP_201_CREATED)
async def manager_create_entry(
    payload: ManagerTipEntryCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:add_for_others"))],
) -> dict:
    # store 권한 체크 — schedule_id 가 있으면 service 가 schedule 의 store 로 derive 하지만
    # 콘솔에서 store_id 가 같이 들어오면 명시 검사. freeform 도 store_id 가 필수.
    if payload.store_id is not None:
        await check_store_access(db, current_user, payload.store_id)
    entry = await tip_service.manager_create_entry(
        db,
        actor=current_user,
        employee_id=payload.employee_id,
        schedule_id=payload.schedule_id,
        store_id=payload.store_id,
        work_role_id=payload.work_role_id,
        work_date=payload.date,
        card_tips=payload.card_tips,
        cash_tips_kept=payload.cash_tips_kept,
        comment=payload.comment,
        distributions=payload.distributions,
    )
    # schedule_id 만 들어온 경우, entry.store_id 가 derived 됐으므로 사후 권한 체크.
    if payload.store_id is None:
        await check_store_access(db, current_user, entry.store_id)
    store_name = await db.scalar(select(Store.name).where(Store.id == entry.store_id))
    return tip_service.build_entry_response(
        entry, store_name=store_name,
        schedule=getattr(entry, "_schedule_loaded", None),
    )


@router.patch("/entries/{entry_id}", response_model=TipEntryResponse)
async def manager_update_entry(
    entry_id: UUID,
    payload: ManagerTipEntryUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:edit_all"))],
) -> dict:
    entry = await tip_service.manager_update_entry(
        db,
        actor=current_user,
        entry_id=entry_id,
        comment=payload.comment,
        card_tips=payload.card_tips,
        cash_tips_kept=payload.cash_tips_kept,
        distributions=payload.distributions,
    )
    await check_store_access(db, current_user, entry.store_id)
    store_name = await db.scalar(select(Store.name).where(Store.id == entry.store_id))
    return tip_service.build_entry_response(
        entry, store_name=store_name,
        schedule=getattr(entry, "_schedule_loaded", None),
    )


@router.get("/periods/dashboard", response_model=PeriodDashboardResponse)
async def get_period_dashboard(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:read"))],
    store_id: Annotated[UUID, Query()],
    date_in_cycle: Annotated[DateType, Query(description="아무 날짜나 — 그 날짜가 속한 사이클 반환")],
) -> dict:
    await check_store_access(db, current_user, store_id)
    return await tip_service.get_period_dashboard(
        db, store_id=store_id, date_in_cycle=date_in_cycle,
    )


@router.post("/periods/confirm", response_model=PeriodDashboardResponse)
async def confirm_period(
    payload: PeriodConfirmRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:period_confirm"))],
    store_id: Annotated[UUID, Query()],
    date_in_cycle: Annotated[DateType, Query()],
) -> dict:
    await check_store_access(db, current_user, store_id)
    await tip_service.confirm_period(
        db, actor=current_user, store_id=store_id, date_in_cycle=date_in_cycle,
    )
    return await tip_service.get_period_dashboard(
        db, store_id=store_id, date_in_cycle=date_in_cycle,
    )


@router.post("/periods/force-close", response_model=PeriodDashboardResponse)
async def force_close_period(
    payload: PeriodForceCloseRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:period_override"))],
    store_id: Annotated[UUID, Query()],
    date_in_cycle: Annotated[DateType, Query()],
) -> dict:
    await check_store_access(db, current_user, store_id)
    await tip_service.confirm_period(
        db, actor=current_user, store_id=store_id, date_in_cycle=date_in_cycle,
        override_reason=payload.reason,
    )
    return await tip_service.get_period_dashboard(
        db, store_id=store_id, date_in_cycle=date_in_cycle,
    )


@router.get("/audit-logs", response_model=list[AuditLogResponse])
async def list_audit_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:read"))],
    store_id: Annotated[Optional[UUID], Query()] = None,
    entity_type: Annotated[Optional[str], Query()] = None,
    action: Annotated[Optional[str], Query()] = None,
    actor_id: Annotated[Optional[UUID], Query()] = None,
    limit: int = 200,
) -> list[dict]:
    if store_id is not None:
        await check_store_access(db, current_user, store_id)
    return await tip_service.query_audit_logs(
        db, store_id=store_id, entity_type=entity_type, action=action,
        actor_id=actor_id, limit=limit,
    )


@router.get("/forms", response_model=list[Form4070Response])
async def list_store_forms(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:form_view"))],
    store_id: Annotated[UUID, Query()],
    date_in_cycle: Annotated[DateType, Query()],
) -> list[dict]:
    """매장의 confirmed period 에 대한 직원별 4070 폼."""
    from app.models.tip import TipPeriod
    from app.models.user import User as UserModel
    from app.services.storage_service import storage_service
    from app.services.tip_service import cycle_for_date

    await check_store_access(db, current_user, store_id)
    start, end = cycle_for_date(date_in_cycle)
    period = await db.scalar(
        select(TipPeriod).where(
            TipPeriod.store_id == store_id,
            TipPeriod.start_date == start,
            TipPeriod.end_date == end,
        )
    )
    if period is None:
        return []
    forms = await tip_service.list_forms_for_period(db, period_id=period.id)
    if not forms:
        return []
    emp_ids = [f.employee_id for f in forms]
    emp_names = {
        uid: name for uid, name in (await db.execute(
            select(UserModel.id, UserModel.full_name).where(UserModel.id.in_(emp_ids))
        )).all()
    }
    store_name = await db.scalar(select(Store.name).where(Store.id == store_id))
    return [
        {
            "id": f.id,
            "employee_id": f.employee_id,
            "employee_name": emp_names.get(f.employee_id),
            "period_id": f.period_id,
            "period_start": period.start_date,
            "period_end": period.end_date,
            "store_id": store_id,
            "store_name": store_name,
            "pdf_key": f.pdf_key,
            "pdf_url": storage_service.resolve_url(f.pdf_key) if f.pdf_key else None,
            "reported_cash": f.reported_cash,
            "reported_card": f.reported_card,
            "paid_out": f.paid_out,
            "net_tips": f.net_tips,
            "status": f.status,
            "generated_at": f.generated_at,
            "signed_at": f.signed_at,
            "signature_image_key": f.signature_image_key,
            "signature_url": storage_service.resolve_url(f.signature_image_key) if f.signature_image_key else None,
            "signature_strokes": f.signature_strokes,
        }
        for f in forms
    ]


@router.post("/forms/{form_id}/remind", status_code=200)
async def remind_unsigned(
    form_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:edit_all"))],
) -> dict:
    """미서명 폼 직원에게 alert 발송 (관리자 액션). 매장 권한 검사 포함."""
    from app.models.alert import Alert
    from app.models.tip import Form4070Document, TipPeriod
    form = await db.scalar(
        select(Form4070Document).where(Form4070Document.id == form_id)
    )
    if form is None or form.status == "signed":
        return {"sent": False}
    # 폼이 어떤 매장의 사이클인지 확인 후 매장 권한 검사.
    period = await db.scalar(
        select(TipPeriod).where(TipPeriod.id == form.period_id)
    )
    if period is None:
        return {"sent": False}
    await check_store_access(db, current_user, period.store_id)
    emp_org = await db.scalar(
        select(User.organization_id).where(User.id == form.employee_id)
    )
    if emp_org is None:
        return {"sent": False}
    db.add(Alert(
        organization_id=emp_org,
        user_id=form.employee_id,
        type="tip_form_remind",
        message="Please sign your IRS Form 4070 — your manager is waiting.",
        reference_type="form_4070",
        reference_id=form.id,
    ))
    await db.commit()
    return {"sent": True}


@router.get("/distributions", response_model=list[StoreDistributionResponse])
async def list_store_distributions(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:read"))],
    store_id: Annotated[UUID, Query()],
    status_filter: Annotated[Optional[str], Query(alias="status")] = None,
    start: Annotated[Optional[DateType], Query()] = None,
    end: Annotated[Optional[DateType], Query()] = None,
    limit: int = 200,
) -> list[dict]:
    await check_store_access(db, current_user, store_id)
    return await tip_service.list_store_distributions(
        db, store_id=store_id, status_filter=status_filter,
        start=start, end=end, limit=limit,
    )
