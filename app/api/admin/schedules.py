"""관리자 스케줄 라우터."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import hide_cost_for, require_permission, scrub_cost_fields
from app.database import get_db
from app.models.user import User
from app.schemas.schedule import (
    BulkAssignChecklistRequest, BulkAssignChecklistResult,
    FinalizeResult, ScheduleAuditLogResponse, ScheduleBulkCreate, ScheduleBulkResult,
    ScheduleCancel, ScheduleCreate,
    ScheduleResponse, ScheduleSwap, ScheduleUpdate, ScheduleValidation,
    ScheduleConfirm, ScheduleReject, ScheduleBulkConfirm, ScheduleBulkConfirmResult,
    ScheduleHistoryListResponse,
)
from app.services.schedule_service import schedule_service

router: APIRouter = APIRouter()


def _scrub(resp, user: User):
    """단일 ScheduleResponse cost redact (SV/Staff)."""
    if hide_cost_for(user):
        scrub_cost_fields(resp)
    return resp


@router.get("", response_model=dict)
async def list_entries(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
    store_id: str | None = None,
    user_id: str | None = None,
    user_ids: str | None = None,  # CSV — 여러 user 동시 조회 (calendar에서 다른 매장 schedule 까지 가져오기 위함)
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    page: int = 1,
    per_page: int = 100,
) -> dict:
    """스케줄 목록. user_ids는 CSV로 여러 user를 한 번에 조회 가능."""
    parsed_user_ids: list[UUID] | None = None
    if user_ids:
        parsed_user_ids = [UUID(x) for x in user_ids.split(",") if x.strip()]
    items, total = await schedule_service.list_entries(
        db, current_user.organization_id,
        store_id=UUID(store_id) if store_id else None,
        user_id=UUID(user_id) if user_id else None,
        user_ids=parsed_user_ids,
        date_from=date_from, date_to=date_to,
        status=status, page=page, per_page=per_page,
    )
    if hide_cost_for(current_user):
        for item in items:
            scrub_cost_fields(item)
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.post("", response_model=ScheduleResponse, status_code=201)
async def create_entry(
    data: ScheduleCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:create"))],
) -> ScheduleResponse:
    """단일 스케줄 생성."""
    return _scrub(await schedule_service.create_entry(
        db, current_user.organization_id, data, current_user.id,
    ), current_user)


@router.post("/bulk", response_model=ScheduleBulkResult, status_code=200)
async def bulk_create(
    data: ScheduleBulkCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:create"))],
) -> ScheduleBulkResult:
    """벌크 스케줄 생성. skip_on_conflict=true면 겹치는 건은 건너뛰고 나머지 생성."""
    return await schedule_service.bulk_create(
        db, current_user.organization_id, data.entries, current_user.id,
        skip_on_conflict=data.skip_on_conflict,
    )


@router.post("/generate-from-requests", response_model=list[ScheduleResponse], status_code=201)
async def generate_from_requests(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:create"))],
    store_id: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[ScheduleResponse]:
    """신청 기반 스케줄 자동생성."""
    from app.utils.exceptions import BadRequestError
    if not store_id or date_from is None or date_to is None:
        raise BadRequestError("store_id, date_from, date_to are required")
    results = await schedule_service.generate_from_requests(
        db, current_user.organization_id, UUID(store_id), date_from, date_to, current_user.id,
    )
    if hide_cost_for(current_user):
        for r in results:
            scrub_cost_fields(r)
    return results


@router.post("/bulk-confirm", response_model=ScheduleBulkConfirmResult, status_code=200)
async def bulk_confirm(
    data: ScheduleBulkConfirm,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> ScheduleBulkConfirmResult:
    """기간 내 모든 requested 스케줄 일괄 확정."""
    return await schedule_service.bulk_confirm(
        db,
        organization_id=current_user.organization_id,
        store_id=UUID(data.store_id),
        date_from=data.date_from,
        date_to=data.date_to,
        approved_by=current_user.id,
    )


@router.get("/history", response_model=ScheduleHistoryListResponse)
async def list_history(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
    store_id: str | None = None,
    user_id: str | None = None,
    actor_id: str | None = None,
    event_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = 1,
    per_page: int = 50,
) -> ScheduleHistoryListResponse:
    """집계 schedule history. GM+ only. SV/Staff은 cost diff 항목 redact."""
    return await schedule_service.list_history(
        db, current_user.organization_id,
        actor=current_user,
        store_id=UUID(store_id) if store_id else None,
        user_id=UUID(user_id) if user_id else None,
        actor_id=UUID(actor_id) if actor_id else None,
        event_type=event_type,
        date_from=date_from, date_to=date_to,
        page=page, per_page=per_page,
    )


@router.get("/{entry_id}", response_model=ScheduleResponse)
async def get_entry(
    entry_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
) -> ScheduleResponse:
    """스케줄 상세."""
    return _scrub(await schedule_service.get_entry(db, entry_id, current_user.organization_id), current_user)


@router.patch("/{entry_id}", response_model=ScheduleResponse)
async def update_entry(
    entry_id: UUID,
    data: ScheduleUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> ScheduleResponse:
    """스케줄 수정. confirmed 스케줄은 GM+ 만 수정 가능."""
    return _scrub(await schedule_service.update_entry(
        db, entry_id, current_user.organization_id, data, actor=current_user,
    ), current_user)


@router.delete("/{entry_id}", status_code=204)
async def delete_entry(
    entry_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:delete"))],
) -> None:
    """스케줄 삭제. confirmed 스케줄은 GM+ 만 삭제 가능."""
    await schedule_service.delete_entry(
        db, entry_id, current_user.organization_id, actor=current_user,
    )


@router.post("/{entry_id}/submit", response_model=ScheduleResponse)
async def submit_schedule(
    entry_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> ScheduleResponse:
    """draft → requested 전환 (제출)."""
    return _scrub(await schedule_service.submit_schedule(
        db, entry_id, current_user.organization_id, current_user,
    ), current_user)


@router.post("/{entry_id}/confirm", response_model=ScheduleResponse)
async def confirm_schedule(
    entry_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> ScheduleResponse:
    """requested 스케줄 확정 (requested → confirmed)."""
    return _scrub(await schedule_service.confirm_schedule(
        db, entry_id, current_user.organization_id, current_user.id,
    ), current_user)


@router.post("/{entry_id}/approve", response_model=ScheduleResponse)
async def approve_schedule(
    entry_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> ScheduleResponse:
    """requested → confirmed (confirm의 alias, 목업 명명 호환)."""
    return _scrub(await schedule_service.confirm_schedule(
        db, entry_id, current_user.organization_id, current_user.id,
    ), current_user)


@router.post("/{entry_id}/reject", response_model=ScheduleResponse)
async def reject_schedule(
    entry_id: UUID,
    data: ScheduleReject,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> ScheduleResponse:
    """requested → rejected. 사유 필수."""
    return _scrub(await schedule_service.reject_schedule(
        db, entry_id, current_user.organization_id, data, actor=current_user,
    ), current_user)


@router.post("/{entry_id}/revert", response_model=ScheduleResponse)
async def revert_schedule(
    entry_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> ScheduleResponse:
    """confirmed → requested (GM+ only)."""
    return _scrub(await schedule_service.revert_schedule(
        db, entry_id, current_user.organization_id, current_user,
    ), current_user)


@router.post("/{entry_id}/cancel", response_model=ScheduleResponse)
async def cancel_schedule(
    entry_id: UUID,
    data: ScheduleCancel,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> ScheduleResponse:
    """confirmed → cancelled (GM+ only). 사유 필수."""
    return _scrub(await schedule_service.cancel_schedule(
        db, entry_id, current_user.organization_id, data, actor=current_user,
    ), current_user)


@router.post("/{entry_id}/swap", response_model=dict)
async def swap_schedule(
    entry_id: UUID,
    data: ScheduleSwap,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> dict:
    """두 confirmed 스케줄의 user_id 교환 (GM+ only)."""
    a, b = await schedule_service.swap_schedules(
        db, entry_id, current_user.organization_id, data, actor=current_user,
    )
    if hide_cost_for(current_user):
        scrub_cost_fields(a)
        scrub_cost_fields(b)
    return {"a": a, "b": b}


@router.delete("/history/{log_id}", status_code=204)
async def delete_history_entry(
    log_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
) -> None:
    """History entry 삭제. Owner only (priority <= 10)."""
    await schedule_service.delete_history_entry(
        db, log_id, current_user.organization_id, actor=current_user,
    )


@router.get("/{entry_id}/audit", response_model=list[ScheduleAuditLogResponse])
async def get_audit_log(
    entry_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
) -> list[ScheduleAuditLogResponse]:
    """스케줄 audit log 조회 (timestamp DESC). SV/Staff는 cost diff 항목 숨김."""
    return await schedule_service.get_audit_log(
        db, entry_id, current_user.organization_id, actor=current_user,
    )


@router.post("/validate", response_model=ScheduleValidation)
async def validate_entry(
    data: ScheduleCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:read"))],
) -> ScheduleValidation:
    """검증만 (저장 안함)."""
    return await schedule_service.validate_entry(db, current_user.organization_id, data)


@router.post("/assign-checklist", response_model=BulkAssignChecklistResult, status_code=200)
async def bulk_assign_checklist(
    data: BulkAssignChecklistRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("schedules:update"))],
) -> BulkAssignChecklistResult:
    """스케줄 일괄 체크리스트 할당/교체/제거.

    Bulk assign, replace, or remove checklist instances for the given schedules.
    - checklist_template_id provided: create or replace cl_instance for each schedule
    - checklist_template_id is null: remove existing cl_instances for each schedule
    """
    return await schedule_service.bulk_assign_checklist(
        db,
        organization_id=current_user.organization_id,
        schedule_ids=[UUID(sid) for sid in data.schedule_ids],
        checklist_template_id=UUID(data.checklist_template_id) if data.checklist_template_id else None,
    )
