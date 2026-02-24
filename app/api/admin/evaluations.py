"""관리자 평가 라우터 — 평가 템플릿 및 평가 관리 API.

Admin Evaluation Router — API endpoints for evaluation template and evaluation management.

Permission Matrix:
    - 템플릿 생성/수정/삭제: Owner + GM
    - 평가 생성/제출: Owner + GM + SV (방향 검증: 상위→하위)
    - 조회: Owner + GM + SV
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_gm, require_supervisor
from app.database import get_db
from app.models.user import User
from app.schemas.common import MessageResponse, PaginatedResponse
from app.schemas.evaluation import (
    EvalTemplateCreate,
    EvalTemplateResponse,
    EvalTemplateUpdate,
    EvaluationCreate,
    EvaluationResponse,
)
from app.services.evaluation_service import evaluation_service

router: APIRouter = APIRouter()


# === 템플릿 CRUD ===

@router.get("/templates", response_model=PaginatedResponse)
async def list_templates(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """평가 템플릿 목록을 조회합니다."""
    templates, total = await evaluation_service.list_templates(
        db, organization_id=current_user.organization_id, page=page, per_page=per_page
    )
    items = [evaluation_service.build_template_response(t) for t in templates]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/templates/{template_id}", response_model=EvalTemplateResponse)
async def get_template(
    template_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """평가 템플릿 상세를 조회합니다."""
    template = await evaluation_service.get_template(
        db, template_id=template_id, organization_id=current_user.organization_id
    )
    return evaluation_service.build_template_response(template)


@router.post("/templates", response_model=EvalTemplateResponse, status_code=201)
async def create_template(
    data: EvalTemplateCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> dict:
    """새 평가 템플릿을 생성합니다. Owner + GM만 가능."""
    template = await evaluation_service.create_template(
        db, organization_id=current_user.organization_id, data=data
    )
    await db.commit()
    return evaluation_service.build_template_response(template)


@router.put("/templates/{template_id}", response_model=EvalTemplateResponse)
async def update_template(
    template_id: UUID,
    data: EvalTemplateUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> dict:
    """평가 템플릿을 수정합니다. Owner + GM만 가능."""
    template = await evaluation_service.update_template(
        db, template_id=template_id, organization_id=current_user.organization_id, data=data
    )
    await db.commit()
    return evaluation_service.build_template_response(template)


@router.delete("/templates/{template_id}", response_model=MessageResponse)
async def delete_template(
    template_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> dict:
    """평가 템플릿을 삭제합니다. Owner + GM만 가능."""
    await evaluation_service.delete_template(
        db, template_id=template_id, organization_id=current_user.organization_id
    )
    await db.commit()
    return {"message": "평가 템플릿이 삭제되었습니다 (Evaluation template deleted)"}


# === 평가 CRUD ===

@router.get("", response_model=PaginatedResponse)
async def list_evaluations(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
    evaluator_id: Annotated[str | None, Query()] = None,
    evaluatee_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """평가 목록을 조회합니다."""
    evaluations, total = await evaluation_service.list_evaluations(
        db,
        organization_id=current_user.organization_id,
        evaluator_id=UUID(evaluator_id) if evaluator_id else None,
        evaluatee_id=UUID(evaluatee_id) if evaluatee_id else None,
        status=status,
        page=page,
        per_page=per_page,
    )
    items = []
    for e in evaluations:
        items.append(await evaluation_service.build_evaluation_response(db, e))
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/{evaluation_id}", response_model=EvaluationResponse)
async def get_evaluation(
    evaluation_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """평가 상세를 조회합니다."""
    evaluation = await evaluation_service.get_evaluation(
        db, evaluation_id=evaluation_id, organization_id=current_user.organization_id
    )
    return await evaluation_service.build_evaluation_response(db, evaluation)


@router.post("", response_model=EvaluationResponse, status_code=201)
async def create_evaluation(
    data: EvaluationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """새 평가를 생성합니다. 방향 검증: 상위→하위."""
    evaluation = await evaluation_service.create_evaluation(
        db,
        organization_id=current_user.organization_id,
        evaluator_id=current_user.id,
        data=data,
    )
    await db.commit()
    return await evaluation_service.build_evaluation_response(db, evaluation)


@router.post("/{evaluation_id}/submit", response_model=EvaluationResponse)
async def submit_evaluation(
    evaluation_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """평가를 제출합니다 (draft → submitted)."""
    evaluation = await evaluation_service.submit_evaluation(
        db, evaluation_id=evaluation_id, organization_id=current_user.organization_id
    )
    await db.commit()
    return await evaluation_service.build_evaluation_response(db, evaluation)
