"""앱 추가 업무 라우터 — 사용자용 추가 업무 API.

App Task Router — API endpoints for user's additional task management.
Provides read access and task completion for the mobile app.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.common import MessageResponse, PaginatedResponse, TaskResponse
from app.services.task_service import task_service

router: APIRouter = APIRouter()


@router.get("/", response_model=PaginatedResponse)
async def list_my_tasks(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """내게 배정된 추가 업무 목록을 조회합니다.

    List additional tasks assigned to the current user.

    Args:
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)
        page: 페이지 번호 (Page number)
        per_page: 페이지당 항목 수 (Items per page)

    Returns:
        dict: 페이지네이션된 업무 목록 (Paginated task list)
    """
    tasks, total = await task_service.list_my_tasks(
        db,
        user_id=current_user.id,
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
async def get_my_task(
    task_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """내 추가 업무 상세를 조회합니다.

    Get my additional task detail.

    Args:
        task_id: 업무 UUID (Task UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)

    Returns:
        dict: 업무 상세 (Task detail)
    """
    task = await task_service.get_detail(
        db,
        task_id=task_id,
        organization_id=current_user.organization_id,
    )
    return await task_service.build_response(db, task)


@router.patch("/{task_id}/complete", response_model=TaskResponse)
async def complete_my_task(
    task_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """내 추가 업무를 완료 처리합니다.

    Mark my additional task as completed.

    Args:
        task_id: 업무 UUID (Task UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 사용자 (Authenticated user)

    Returns:
        dict: 완료된 업무 상세 (Completed task detail)
    """
    task = await task_service.complete_my_task(
        db,
        task_id=task_id,
        user_id=current_user.id,
        organization_id=current_user.organization_id,
    )
    await db.commit()

    # 담당자 포함 상세 다시 조회 — Re-fetch with assignees loaded
    task = await task_service.get_detail(
        db,
        task_id=task.id,
        organization_id=current_user.organization_id,
    )
    return await task_service.build_response(db, task)
