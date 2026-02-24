"""앱 체크리스트 인스턴스 라우터 — 내 체크리스트 API.

App Checklist Instance Router — API endpoints for user's own checklist instances.
Provides read access and item completion for the mobile app.
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
from app.services.checklist_instance_service import checklist_instance_service
from app.utils.exceptions import ForbiddenError

router: APIRouter = APIRouter()


@router.get("", response_model=list[ChecklistInstanceResponse])
async def list_my_checklist_instances(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    work_date: Annotated[date | None, Query()] = None,
) -> list[dict]:
    """내 체크리스트 인스턴스 목록을 조회합니다.

    List my checklist instances with optional date filter.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)
        work_date: 근무일 필터, 선택 (Optional work date filter)

    Returns:
        list[dict]: 내 인스턴스 목록 (My instance list)
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
    """내 체크리스트 인스턴스 상세를 조회합니다.

    Get my checklist instance detail with snapshot merged with completions.

    Args:
        instance_id: 인스턴스 UUID (Instance UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)

    Returns:
        dict: 인스턴스 상세 (Instance detail with merged snapshot)
    """
    instance = await checklist_instance_service.get_instance(
        db,
        instance_id=instance_id,
    )

    # 본인 인스턴스만 조회 가능 — Only allow viewing own instances
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
    """체크리스트 항목을 완료 처리합니다.

    Complete a checklist item in an instance.

    Args:
        instance_id: 인스턴스 UUID (Instance UUID)
        item_index: 체크리스트 항목 인덱스 (Checklist item index)
        data: 완료 데이터 (Completion data: photo_url, note, location)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)

    Returns:
        dict: 업데이트된 인스턴스 상세 (Updated instance detail)
    """
    instance = await checklist_instance_service.complete_item(
        db,
        instance_id=instance_id,
        item_index=item_index,
        user_id=current_user.id,
        photo_url=data.photo_url,
        note=data.note,
        location=data.location,
        client_timezone=data.timezone,
    )
    await db.commit()

    return await checklist_instance_service.build_detail_response(db, instance)
