"""직원용 팁 API (Stage A).

엔드포인트:
    POST   /api/v1/app/my/tips/entries                  — entry + nested 분배 생성
    PATCH  /api/v1/app/my/tips/entries/{entry_id}       — 본인 entry 수정
    GET    /api/v1/app/my/tips/entries?start=&end=      — 본인 일별 entries
    GET    /api/v1/app/my/tips/distributions/incoming   — 내가 받은 분배
    POST   /api/v1/app/my/tips/distributions/{id}/accept — OK 처리

매니저용 console API 는 Stage B 에서 추가.
"""

from __future__ import annotations

from datetime import date as DateType
from typing import Annotated, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, require_permission
from app.database import get_db
from app.models.organization import Store
from app.models.user import User
from app.schemas.tip import (
    Form4070Response,
    SignatureResponse,
    SignatureUpdateRequest,
    SignFormRequest,
    TipDistributionIncomingResponse,
    TipDistributionResponse,
    TipEntryCreate,
    TipEntryResponse,
    TipEntryUpdate,
)
from app.services.tip_service import tip_service


router: APIRouter = APIRouter()


# ── Entries ─────────────────────────────────────────────────────

@router.post("/entries", response_model=TipEntryResponse, status_code=status.HTTP_201_CREATED)
async def create_my_entry(
    payload: TipEntryCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:edit_own"))],
) -> dict:
    """본인 entry 생성 — schedule_id 기반. store/work_role/date 는 schedule 에서 derive."""
    entry = await tip_service.create_entry(db, actor=current_user, payload=payload)
    # schedule 의 store 에 본인이 user_stores 매핑이 없을 수 있으므로 사후 권한 검사.
    await check_store_access(db, current_user, entry.store_id)
    store_name = await db.scalar(select(Store.name).where(Store.id == entry.store_id))
    entry = await tip_service._get_entry_with_dists(db, entry.id)
    return tip_service.build_entry_response(
        entry, store_name=store_name, schedule=getattr(entry, "_schedule_loaded", None),
    )


@router.patch("/entries/{entry_id}", response_model=TipEntryResponse)
async def update_my_entry(
    entry_id: UUID,
    payload: TipEntryUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:edit_own"))],
) -> dict:
    entry = await tip_service.update_entry(
        db, actor=current_user, entry_id=entry_id, payload=payload
    )
    store_name = await db.scalar(select(Store.name).where(Store.id == entry.store_id))
    return tip_service.build_entry_response(
        entry, store_name=store_name, schedule=getattr(entry, "_schedule_loaded", None),
    )


@router.get("/entries", response_model=list[TipEntryResponse])
async def list_my_entries(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:read"))],
    start: Annotated[DateType, Query(description="범위 시작일 (포함)")],
    end: Annotated[DateType, Query(description="범위 종료일 (포함)")],
    store_id: Annotated[Optional[UUID], Query()] = None,
) -> list[dict]:
    entries = await tip_service.list_my_entries(
        db, employee_id=current_user.id, start=start, end=end, store_id=store_id,
    )
    if not entries:
        return []
    # store 이름 batch 로드
    store_ids = {e.store_id for e in entries}
    store_rows = (await db.execute(
        select(Store.id, Store.name).where(Store.id.in_(store_ids))
    )).all()
    name_by_id = {sid: name for sid, name in store_rows}
    out: list[dict] = []
    for e in entries:
        note, by_name = await tip_service.latest_manager_note(
            db, entry_id=e.id, employee_id=current_user.id,
        )
        out.append(tip_service.build_entry_response(
            e,
            store_name=name_by_id.get(e.store_id),
            schedule=getattr(e, "_schedule_loaded", None),
            last_manager_note=note,
            last_modified_by_name=by_name,
        ))
    return out


# ── Eligible receivers ──────────────────────────────────────────

@router.get("/entries/eligible-receivers")
async def list_eligible_receivers(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:edit_own"))],
    schedule_id: Annotated[UUID, Query()],
) -> list[dict]:
    """본 schedule 의 분배 대상 후보 — 같은 매장 + 같은 work_date + status=confirmed.

    본인 attendance 의 clock_in/clock_out 시간대와 겹치는 사람만. attendance
    가 없으면 schedule 시간으로 fallback. 본인 제외.
    """
    return await tip_service.get_eligible_receivers(
        db,
        schedule_id=schedule_id,
        asking_user_id=current_user.id,
        organization_id=current_user.organization_id,
    )


# ── Distributions ───────────────────────────────────────────────

@router.get(
    "/distributions/incoming",
    response_model=list[TipDistributionIncomingResponse],
)
async def list_incoming_distributions(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:read"))],
    status_filter: Annotated[Optional[str], Query(alias="status")] = None,
    limit: int = 100,
) -> list[dict]:
    return await tip_service.list_incoming(
        db, receiver_id=current_user.id, status=status_filter, limit=limit,
    )


@router.get("/forms", response_model=list[Form4070Response])
async def list_my_forms(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:form_view"))],
) -> list[dict]:
    from app.models.tip import TipPeriod
    from app.services.storage_service import storage_service
    forms = await tip_service.list_forms_for_employee(db, employee_id=current_user.id)
    if not forms:
        return []
    period_ids = {f.period_id for f in forms}
    periods = {
        p.id: p for p in (await db.scalars(
            select(__import__('app.models.tip', fromlist=['TipPeriod']).TipPeriod).where(
                __import__('app.models.tip', fromlist=['TipPeriod']).TipPeriod.id.in_(period_ids)
            )
        )).all()
    } if period_ids else {}
    store_ids = {p.store_id for p in periods.values()}
    stores = {
        sid: name for sid, name in (await db.execute(
            select(Store.id, Store.name).where(Store.id.in_(store_ids))
        )).all()
    } if store_ids else {}
    out: list[dict] = []
    for f in forms:
        p = periods.get(f.period_id)
        out.append({
            "id": f.id,
            "employee_id": f.employee_id,
            "period_id": f.period_id,
            "period_start": p.start_date if p else None,
            "period_end": p.end_date if p else None,
            "store_id": p.store_id if p else None,
            "store_name": stores.get(p.store_id) if p else None,
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
        })
    return out


@router.post("/forms/{form_id}/sign", response_model=Form4070Response)
async def sign_form(
    form_id: UUID,
    payload: SignFormRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:form_view"))],
) -> dict:
    from app.models.tip import TipPeriod
    from app.services.storage_service import storage_service
    form = await tip_service.sign_form(
        db,
        actor=current_user,
        form_id=form_id,
        signature_image_key=payload.signature_image_key,
        save_for_future=payload.save_for_future,
    )
    period = await db.scalar(select(TipPeriod).where(TipPeriod.id == form.period_id))
    store_name = (
        await db.scalar(select(Store.name).where(Store.id == period.store_id))
        if period is not None else None
    )
    return {
        "id": form.id,
        "employee_id": form.employee_id,
        "period_id": form.period_id,
        "period_start": period.start_date if period else None,
        "period_end": period.end_date if period else None,
        "store_id": period.store_id if period else None,
        "store_name": store_name,
        "pdf_key": form.pdf_key,
        "pdf_url": storage_service.resolve_url(form.pdf_key) if form.pdf_key else None,
        "reported_cash": form.reported_cash,
        "reported_card": form.reported_card,
        "paid_out": form.paid_out,
        "net_tips": form.net_tips,
        "status": form.status,
        "generated_at": form.generated_at,
        "signed_at": form.signed_at,
        "signature_image_key": form.signature_image_key,
        "signature_url": storage_service.resolve_url(form.signature_image_key) if form.signature_image_key else None,
    }


# ── Signature ──────────────────────────────────────────────


@router.get("/signature", response_model=SignatureResponse)
async def get_my_signature(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:read"))],
) -> dict:
    from app.services.storage_service import storage_service
    user = await db.scalar(select(User).where(User.id == current_user.id))
    key = user.signature_image_key if user else None
    return {
        "signature_image_key": key,
        "signature_url": storage_service.resolve_url(key) if key else None,
    }


@router.post("/signature/blob", response_model=SignatureResponse)
async def upload_signature_blob(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:read"))],
) -> dict:
    """raw image bytes (PNG) 를 받아 storage 에 저장하고 key 반환.

    Body: raw PNG bytes (Content-Type: image/png).
    저장 위치: signatures/users/{user_id}/{uuid}.png
    """
    import uuid as _uuid

    from app.services.storage_service import storage_service
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty body")
    key = f"signatures/users/{current_user.id}/{_uuid.uuid4()}.png"
    storage_service.save_local(key, body)
    return {
        "signature_image_key": key,
        "signature_url": storage_service.resolve_url(key),
    }


@router.put("/signature", response_model=SignatureResponse)
async def update_my_signature(
    payload: SignatureUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:read"))],
) -> dict:
    from app.services.storage_service import storage_service
    user = await db.scalar(select(User).where(User.id == current_user.id))
    if user is None:
        return {"signature_image_key": None}
    user.signature_image_key = payload.signature_image_key
    await db.commit()
    return {
        "signature_image_key": user.signature_image_key,
        "signature_url": storage_service.resolve_url(user.signature_image_key),
    }


@router.delete("/signature", status_code=204, response_class=Response)
async def clear_my_signature(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:read"))],
) -> Response:
    user = await db.scalar(select(User).where(User.id == current_user.id))
    if user is not None:
        user.signature_image_key = None
        await db.commit()
    return Response(status_code=204)


@router.post("/distributions/{distribution_id}/accept", response_model=TipDistributionResponse)
async def accept_distribution(
    distribution_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tips:read"))],
) -> dict:
    dist = await tip_service.accept_distribution(
        db, actor=current_user, distribution_id=distribution_id,
    )
    return {
        "id": dist.id,
        "entry_id": dist.entry_id,
        "receiver_id": dist.receiver_id,
        "receiver_name": dist.receiver_name_snapshot,
        "amount": dist.amount,
        "reason": dist.reason,
        "status": dist.status,
        "pending_until": dist.pending_until,
        "accepted_at": dist.accepted_at,
        "created_at": dist.created_at,
    }
