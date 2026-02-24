"""관리자 추가 업무 라우터 — 추가 업무 관리 API.

Admin Task Router — API endpoints for additional task management.
Provides CRUD operations with assignee management and filtering.

Permission Matrix (역할별 권한 설계):
    - Task 생성/수정/삭제: Owner + GM (담당 매장)
    - Task 조회: Owner + GM + SV (소속 매장)
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_accessible_store_ids, require_gm, require_supervisor
from app.database import get_db
from app.models.user import User
from app.schemas.common import (
    MessageResponse,
    PaginatedResponse,
    TaskCreate,
    TaskEvidenceResponse,
    TaskResponse,
    TaskUpdate,
)
from app.services.task_service import task_service
from app.services.task_evidence_service import task_evidence_service

router: APIRouter = APIRouter()


@router.get("", response_model=PaginatedResponse)
async def list_tasks(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
    store_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    priority: Annotated[str | None, Query()] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """추가 업무 목록을 필터링하여 조회합니다. 접근 가능한 매장만 표시.

    List additional tasks with optional filters. Scoped to accessible stores.
    """
    store_uuid: UUID | None = UUID(store_id) if store_id else None
    accessible = await get_accessible_store_ids(db, current_user)

    # 특정 매장 필터가 있으면 접근 권한 확인 — Validate store filter against access scope
    if store_uuid is not None and accessible is not None and store_uuid not in accessible:
        return {"items": [], "total": 0, "page": page, "per_page": per_page}

    tasks, total = await task_service.list_tasks(
        db,
        organization_id=current_user.organization_id,
        store_id=store_uuid,
        status=status,
        priority=priority,
        page=page,
        per_page=per_page,
        accessible_store_ids=accessible,
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
    """
    task = await task_service.get_detail(
        db,
        task_id=task_id,
        organization_id=current_user.organization_id,
    )
    return await task_service.build_response(db, task)


@router.post("", response_model=TaskResponse, status_code=201)
async def create_task(
    data: TaskCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> dict:
    """새 추가 업무를 생성합니다. Owner + GM만 가능.

    Create a new additional task with assignees. Owner + GM only.
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
    current_user: Annotated[User, Depends(require_gm)],
) -> dict:
    """추가 업무를 업데이트합니다. Owner + GM만 가능.

    Update an additional task. Owner + GM only.
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
    current_user: Annotated[User, Depends(require_gm)],
) -> dict:
    """추가 업무를 삭제합니다. Owner + GM만 가능.

    Delete an additional task. Owner + GM only.
    """
    await task_service.delete_task(
        db,
        task_id=task_id,
        organization_id=current_user.organization_id,
    )
    await db.commit()

    return {"message": "추가 업무가 삭제되었습니다 (Additional task deleted)"}


@router.get("/{task_id}/evidences", response_model=list[TaskEvidenceResponse])
async def list_task_evidences(
    task_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> list[dict]:
    """특정 업무의 증빙 목록을 조회합니다. SV 이상만 가능.

    List all evidences for a specific task. Supervisor+ only.
    """
    # 업무 존재 및 조직 확인 — Verify task exists within the organization
    await task_service.get_detail(
        db,
        task_id=task_id,
        organization_id=current_user.organization_id,
    )
    return await task_evidence_service.get_evidences(db, task_id)
