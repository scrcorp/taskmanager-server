"""Console tasks API (renamed from issues, originally additional_tasks).

기본 CRUD + promote from issue_report.
"""
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
    TaskCreate,
    TaskPromoteRequest,
    TaskResponse,
    TaskTransitionRequest,
    TaskUpdate,
)
from app.services.task_service import task_service
from app.core.permissions import is_gm_plus

router: APIRouter = APIRouter()


@router.get("")
async def list_tasks(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:read"))],
    store_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    tasks, total = await task_service.list_tasks(
        db,
        organization_id=current_user.organization_id,
        store_id=UUID(store_id) if store_id else None,
        status=status,
        category=category,
        page=page,
        per_page=per_page,
    )
    items = await task_service.build_responses_batch(db, tasks)
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:read"))],
) -> dict:
    task = await task_service.get_task(db, task_id, current_user.organization_id)
    return await task_service.build_response(db, task)


@router.post("", status_code=201, response_model=TaskResponse)
async def create_task(
    data: TaskCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:create"))],
) -> dict:
    task = await task_service.create_task(
        db, current_user.organization_id, current_user.id, data
    )
    return await task_service.build_response(db, task)


@router.put("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: UUID,
    data: TaskUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:update"))],
) -> dict:
    task = await task_service.update_task(
        db, task_id, current_user.organization_id, data
    )
    return await task_service.build_response(db, task)


@router.delete("/{task_id}", status_code=204)
async def delete_task(
    task_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:delete"))],
) -> None:
    await task_service.delete_task(db, task_id, current_user.organization_id)


@router.post("/from-report/{report_id}", status_code=201, response_model=TaskResponse)
async def promote_report_to_task(
    report_id: UUID,
    data: TaskPromoteRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:create"))],
) -> dict:
    """이슈 리포트를 work item task로 promote (관리자 액션)."""
    task = await task_service.promote_from_report(
        db, report_id, current_user.organization_id, current_user.id, data
    )
    return await task_service.build_response(db, task)


# ── Status transition (submit / approve / reopen) ───────────────────────
@router.post("/{task_id}/transition", response_model=TaskResponse)
async def transition_task(
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


# ── Comments ────────────────────────────────────────────────────────────
@router.get("/{task_id}/comments", response_model=list[TaskCommentOut])
async def list_task_comments(
    task_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("tasks:read"))],
) -> list[dict]:
    return await task_service.list_comments(db, task_id, current_user.organization_id)


@router.post("/{task_id}/comments", status_code=201, response_model=TaskCommentOut)
async def add_task_comment(
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
