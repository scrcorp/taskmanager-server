"""App (staff) tasks API — 본인 배정된 task 조회/상태 업데이트."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.task import (
    TaskCommentCreate,
    TaskCommentOut,
    TaskResponse,
    TaskTransitionRequest,
    TaskUpdate,
)
from app.services.task_service import task_service
from app.core.permissions import is_gm_plus

router: APIRouter = APIRouter()


@router.get("")
async def list_my_tasks(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:read"))],
    status: Annotated[str | None, Query()] = None,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    tasks, total = await task_service.list_tasks(
        db,
        organization_id=current_user.organization_id,
        assignee_id=current_user.id,
        status=status,
        page=page,
        per_page=per_page,
    )
    items = await task_service.build_responses_batch(db, tasks)
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/{task_id}", response_model=TaskResponse)
async def get_my_task(
    task_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:read"))],
) -> dict:
    task = await task_service.get_task(db, task_id, current_user.organization_id)
    return await task_service.build_response(db, task)


@router.put("/{task_id}", response_model=TaskResponse)
async def update_my_task(
    task_id: UUID,
    data: TaskUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:update"))],
) -> dict:
    """Staff가 자기 task 의 attachments / status 변경."""
    task = await task_service.update_task(
        db, task_id, current_user.organization_id, data
    )
    return await task_service.build_response(db, task)


@router.post("/{task_id}/transition", response_model=TaskResponse)
async def transition_my_task(
    task_id: UUID,
    data: TaskTransitionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:update"))],
) -> dict:
    task = await task_service.transition(
        db,
        task_id,
        current_user.organization_id,
        current_user,
        next_status=data.status,
        comment=data.comment,
        is_manager=is_gm_plus(current_user),
        attachments=data.attachments,
    )
    return await task_service.build_response(db, task)


@router.get("/{task_id}/comments", response_model=list[TaskCommentOut])
async def list_my_task_comments(
    task_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:read"))],
) -> list[dict]:
    return await task_service.list_comments(db, task_id, current_user.organization_id)


@router.post("/{task_id}/comments", status_code=201, response_model=TaskCommentOut)
async def add_my_task_comment(
    task_id: UUID,
    data: TaskCommentCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:update"))],
) -> dict:
    return await task_service.add_comment(
        db,
        task_id,
        current_user.organization_id,
        current_user.id,
        data.content,
        attachments=data.attachments,
    )
