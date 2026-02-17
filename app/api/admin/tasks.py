"""관리자 추가 업무 라우터 — 추가 업무 관리 API.

Admin Task Router — API endpoints for additional task management.
Provides CRUD operations with assignee management and filtering.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_supervisor
from app.database import get_db
from app.models.user import User
from app.schemas.common import (
    MessageResponse,
    PaginatedResponse,
    TaskCreate,
    TaskResponse,
    TaskUpdate,
)
from app.services.task_service import task_service

router: APIRouter = APIRouter()


@router.get("/", response_model=PaginatedResponse)
async def list_tasks(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
    brand_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    priority: Annotated[str | None, Query()] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """추가 업무 목록을 필터링하여 조회합니다.

    List additional tasks with optional filters.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)
        brand_id: 브랜드 UUID 필터, 선택 (Optional brand UUID filter)
        status: 상태 필터, 선택 (Optional status filter)
        priority: 우선순위 필터, 선택 (Optional priority filter)
        page: 페이지 번호 (Page number)
        per_page: 페이지당 항목 수 (Items per page)

    Returns:
        dict: 페이지네이션된 업무 목록 (Paginated task list)
    """
    brand_uuid: UUID | None = UUID(brand_id) if brand_id else None

    tasks, total = await task_service.list_tasks(
        db,
        organization_id=current_user.organization_id,
        brand_id=brand_uuid,
        status=status,
        priority=priority,
        page=page,
        per_page=per_page,
    )

    items: list[dict] = []
    for t in tasks:
        response: dict = await task_service.build_response(db, t)
        items.append(response)

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """추가 업무 상세를 조회합니다.

    Get additional task detail with assignees.

    Args:
        task_id: 업무 UUID 문자열 (Task UUID string)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 업무 상세 (Task detail)
    """
    task = await task_service.get_detail(
        db,
        task_id=task_id,
        organization_id=current_user.organization_id,
    )
    return await task_service.build_response(db, task)


@router.post("/", response_model=TaskResponse, status_code=201)
async def create_task(
    data: TaskCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """새 추가 업무를 생성합니다.

    Create a new additional task with assignees.

    Args:
        data: 업무 생성 데이터 (Task creation data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 생성된 업무 (Created task)
    """
    task = await task_service.create_task(
        db,
        organization_id=current_user.organization_id,
        data=data,
        created_by=current_user.id,
    )
    await db.commit()

    # 담당자 포함 상세 다시 조회 — Re-fetch with assignees loaded
    task = await task_service.get_detail(
        db,
        task_id=task.id,
        organization_id=current_user.organization_id,
    )
    return await task_service.build_response(db, task)


@router.put("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: UUID,
    data: TaskUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """추가 업무를 업데이트합니다.

    Update an additional task.

    Args:
        task_id: 업무 UUID 문자열 (Task UUID string)
        data: 업데이트 데이터 (Update data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 업데이트된 업무 (Updated task)
    """
    task = await task_service.update_task(
        db,
        task_id=task_id,
        organization_id=current_user.organization_id,
        data=data,
    )
    await db.commit()

    # 담당자 포함 상세 다시 조회 — Re-fetch with assignees loaded
    task = await task_service.get_detail(
        db,
        task_id=task.id,
        organization_id=current_user.organization_id,
    )
    return await task_service.build_response(db, task)


@router.delete("/{task_id}", response_model=MessageResponse)
async def delete_task(
    task_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """추가 업무를 삭제합니다.

    Delete an additional task.

    Args:
        task_id: 업무 UUID 문자열 (Task UUID string)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 삭제 결과 메시지 (Deletion result message)
    """
    await task_service.delete_task(
        db,
        task_id=task_id,
        organization_id=current_user.organization_id,
    )
    await db.commit()

    return {"message": "추가 업무가 삭제되었습니다 (Additional task deleted)"}
