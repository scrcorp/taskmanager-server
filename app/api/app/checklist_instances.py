"""앱 체크리스트 인스턴스 라우터 — 내 체크리스트 API.

App Checklist Instance Router — API endpoints for user's own checklist instances.
Provides read access, item completion, resubmission, and review comment for the mobile app.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.common import (
    ChecklistCompletionCreate,
    ChecklistInstanceDetailResponse,
    ChecklistInstanceResponse,
)
from app.schemas.checklist_review import ResubmitRequest, ReviewContentCreate, ReviewContentResponse
from app.services.checklist_instance_service import checklist_instance_service
from app.utils.exceptions import ForbiddenError
from app.utils.timezone import get_store_timezone, resolve_timezone

router: APIRouter = APIRouter()


@router.get("", response_model=list[ChecklistInstanceResponse])
async def list_my_checklist_instances(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    work_date: Annotated[date | None, Query()] = None,
) -> list[dict]:
    """내 체크리스트 인스턴스 목록을 조회합니다.

    List my checklist instances with optional date filter.
    """
    instances = await checklist_instance_service.get_my_instances(
        db,
        user_id=current_user.id,
        work_date=work_date,
    )

    items: list[dict] = []
    for inst in instances:
        response: dict = await checklist_instance_service.build_response(db, inst)
        items.append(response)

    return items


@router.get("/{instance_id}", response_model=ChecklistInstanceDetailResponse)
async def get_my_checklist_instance(
    instance_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """내 체크리스트 인스턴스 상세를 조회합니다."""
    instance = await checklist_instance_service.get_instance(
        db,
        instance_id=instance_id,
    )

    if instance.user_id != current_user.id:
        raise ForbiddenError("본인의 체크리스트만 조회할 수 있습니다 (Can only view your own checklist)")

    return await checklist_instance_service.build_detail_response(db, instance)


@router.post(
    "/{instance_id}/items/{item_index}/complete",
    response_model=ChecklistInstanceDetailResponse,
    status_code=201,
)
async def complete_checklist_item(
    instance_id: UUID,
    item_index: int,
    data: ChecklistCompletionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """체크리스트 항목을 완료 처리합니다."""
    # 인스턴스 조회 후 매장 타임존 해석
    inst = await checklist_instance_service.get_instance(db, instance_id)
    store_tz = await get_store_timezone(db, inst.store_id)
    effective_tz = resolve_timezone(data.timezone, store_tz)

    instance = await checklist_instance_service.complete_item(
        db,
        instance_id=instance_id,
        item_index=item_index,
        user_id=current_user.id,
        photo_url=data.photo_url,
        note=data.note,
        location=data.location,
        client_timezone=effective_tz,
    )
    await db.commit()

    return await checklist_instance_service.build_detail_response(db, instance)


@router.put(
    "/{instance_id}/items/{item_index}/resubmit",
    response_model=ChecklistInstanceDetailResponse,
)
async def resubmit_checklist_item(
    instance_id: UUID,
    item_index: int,
    data: ResubmitRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """체크리스트 항목을 재제출합니다 (Staff용).

    Archives existing evidence, updates with new data,
    sets review to pending_re_review, notifies reviewer.
    """
    # 매장 타임존 해석
    inst = await checklist_instance_service.get_instance(db, instance_id)
    store_tz = await get_store_timezone(db, inst.store_id)
    effective_tz = resolve_timezone(data.client_timezone, store_tz)

    instance = await checklist_instance_service.resubmit_completion(
        db,
        instance_id=instance_id,
        item_index=item_index,
        user_id=current_user.id,
        photo_url=data.photo_url,
        note=data.note,
        location=data.location,
        client_timezone=effective_tz,
    )
    await db.commit()

    return await checklist_instance_service.build_detail_response(db, instance)


@router.post(
    "/{instance_id}/items/{item_index}/review/contents",
    response_model=ReviewContentResponse,
    status_code=201,
)
async def add_review_content_as_staff(
    instance_id: UUID,
    item_index: int,
    data: ReviewContentCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """Staff가 리뷰 코멘트 스레드에 댓글을 추가합니다."""
    # 본인 인스턴스 확인
    instance = await checklist_instance_service.get_instance(db, instance_id)
    if instance.user_id != current_user.id:
        raise ForbiddenError("본인의 체크리스트에만 코멘트를 추가할 수 있습니다")

    rc = await checklist_instance_service.add_review_content(
        db,
        instance_id=instance_id,
        item_index=item_index,
        author_id=current_user.id,
        content_type=data.type,
        content=data.content,
    )
    await db.commit()

    return {
        "id": str(rc.id),
        "review_id": str(rc.review_id),
        "author_id": str(rc.author_id),
        "author_name": current_user.full_name,
        "type": rc.type,
        "content": rc.content,
        "created_at": rc.created_at,
    }
