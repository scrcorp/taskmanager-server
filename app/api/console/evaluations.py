"""관리자 평가 라우터 — Evaluation v1 API.

Admin Evaluation Router — `/api/v1/console/evaluations`.

Routing order: 정적 경로(/templates, /templates/{id}, /evaluatable-users)를
동적 /{evaluation_id} 보다 먼저 등록해야 shadow 되지 않는다 (checklists.py 패턴).

Permission Matrix:
    - 조회(목록/상세/템플릿): evaluations:read
    - 작성/picker: evaluations:create (방향 검증 §5)
    - 수정: evaluations:update
    - 삭제(소프트): evaluations:delete

Store scoping:
    - POST/PUT: check_store_access (불가 → 403)
    - GET /: store_id 필터를 accessible 과 intersect (불가 매장 → 빈 페이지)
    - GET /{id}: 평가의 store_id 접근 가능 / Owner / evaluator 본인만 (아니면 404)
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    check_store_access,
    get_accessible_store_ids,
    require_permission,
)
from app.core.permissions import is_owner
from app.database import get_db
from app.models.user import User
from app.schemas.common import MessageResponse, PaginatedResponse
from app.schemas.evaluation import (
    EvalTemplateResponse,
    EvaluatableUsersPage,
    EvaluationCreate,
    EvaluationResponse,
    EvaluationUpdate,
)
from app.services.evaluation_service import evaluation_service

router: APIRouter = APIRouter()


# ====================================================================
# 정적 경로 — /{evaluation_id} 보다 먼저 등록 (shadow 방지)
# ====================================================================


@router.get("/templates", response_model=list[EvalTemplateResponse])
async def list_templates(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("evaluations:read"))],
) -> list[dict]:
    """평가 템플릿 목록 — v1 은 조직 Basic 1개(read-only)."""
    templates = await evaluation_service.list_templates(
        db, organization_id=current_user.organization_id
    )
    return [evaluation_service.build_template_response(t) for t in templates]


@router.get("/templates/{template_id}", response_model=EvalTemplateResponse)
async def get_template(
    template_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("evaluations:read"))],
) -> dict:
    """평가 템플릿 상세. org 밖/부재 시 404."""
    template = await evaluation_service.get_template(
        db, template_id=template_id, organization_id=current_user.organization_id
    )
    return evaluation_service.build_template_response(template)


@router.get("/evaluatable-users", response_model=EvaluatableUsersPage)
async def list_evaluatable_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("evaluations:create"))],
    store_id: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    page: int = 1,
    limit: int = 30,
) -> dict:
    """평가 가능 직원 picker — 방향 필터(엄격히 낮은 권한) + 매장 스코프 + 검색/페이지.

    store_id 가 주어지면 그 매장 접근 가능 여부를 먼저 검증(불가 → 403).
    q 로 full_name/employee_no 부분일치 서버 검색, page/limit 로 페이지네이션.
    각 후보는 stores[] 에 자신의 모든 매장을 포함한다(§M1 picker dropdown).
    """
    page = max(1, page)
    limit = max(1, min(limit, 100))
    store_uuid: UUID | None = UUID(store_id) if store_id else None
    if store_uuid is not None:
        await check_store_access(db, current_user, store_uuid)
    return await evaluation_service.list_evaluatable_users(
        db, current_user, store_id=store_uuid, q=q, page=page, limit=limit
    )


# ====================================================================
# 평가 CRUD
# ====================================================================


@router.get("/", response_model=PaginatedResponse)
async def list_evaluations(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("evaluations:read"))],
    store_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    evaluatee_id: Annotated[str | None, Query()] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """평가 목록 — org-scope, soft-delete 제외, created_at DESC.

    store_id 필터는 accessible 과 intersect (불가 매장 → 빈 페이지).
    """
    per_page = max(1, min(per_page, 100))
    page = max(1, page)

    accessible = await get_accessible_store_ids(db, current_user)
    store_uuid: UUID | None = UUID(store_id) if store_id else None

    if store_uuid is not None:
        if accessible is not None and store_uuid not in accessible:
            return {"items": [], "total": 0, "page": page, "per_page": per_page}
        store_ids: list[UUID] | None = [store_uuid]
    else:
        store_ids = list(accessible) if accessible is not None else None

    evaluations, total = await evaluation_service.list_evaluations(
        db,
        organization_id=current_user.organization_id,
        store_ids=store_ids,
        status=status,
        evaluatee_id=UUID(evaluatee_id) if evaluatee_id else None,
        page=page,
        per_page=per_page,
    )
    items = [
        await evaluation_service.build_evaluation_response(db, e)
        for e in evaluations
    ]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/{evaluation_id}", response_model=EvaluationResponse)
async def get_evaluation(
    evaluation_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("evaluations:read"))],
) -> dict:
    """평가 상세. org 밖/soft-deleted/부재 시 404.

    추가로 평가의 store 가 접근 불가 + Owner 아님 + evaluator 본인 아님이면 404
    (cross-store 존재 누설 방지).
    """
    evaluation = await evaluation_service.get_evaluation(
        db, evaluation_id=evaluation_id, organization_id=current_user.organization_id
    )

    if not is_owner(current_user) and evaluation.evaluator_id != current_user.id:
        accessible = await get_accessible_store_ids(db, current_user)
        if (
            accessible is not None
            and evaluation.store_id is not None
            and evaluation.store_id not in accessible
        ):
            from app.utils.exceptions import NotFoundError

            raise NotFoundError("Evaluation not found")

    return await evaluation_service.build_evaluation_response(db, evaluation)


@router.post("/", response_model=EvaluationResponse, status_code=201)
async def create_evaluation(
    data: EvaluationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("evaluations:create"))],
) -> dict:
    """새 평가 생성. 방향 검증(상위→하위) + (store 있으면)매장 접근 검증 + 스냅샷.

    draft 는 store 없이 저장 가능(§M6) — store_id 가 있을 때만 접근 검증.
    """
    if data.store_id:
        await check_store_access(db, current_user, UUID(data.store_id))
    evaluation = await evaluation_service.create_evaluation(
        db,
        organization_id=current_user.organization_id,
        evaluator=current_user,
        data=data,
    )
    return await evaluation_service.build_evaluation_response(db, evaluation)


@router.put("/{evaluation_id}", response_model=EvaluationResponse)
async def update_evaluation(
    evaluation_id: UUID,
    data: EvaluationUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("evaluations:update"))],
) -> dict:
    """평가 수정 (draft/submitted). 변경된 evaluatee/store 재검증, submit-gate."""

    async def _check_store_access(store_id: UUID) -> None:
        await check_store_access(db, current_user, store_id)

    evaluation = await evaluation_service.update_evaluation(
        db,
        evaluation_id=evaluation_id,
        organization_id=current_user.organization_id,
        current_user=current_user,
        data=data,
        check_store_access=_check_store_access,
    )
    return await evaluation_service.build_evaluation_response(db, evaluation)


@router.delete("/{evaluation_id}", response_model=MessageResponse)
async def delete_evaluation(
    evaluation_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("evaluations:delete"))],
) -> dict:
    """평가 소프트 삭제. 이미 삭제/부재면 404 (idempotent-safe)."""
    await evaluation_service.delete_evaluation(
        db, evaluation_id=evaluation_id, organization_id=current_user.organization_id
    )
    return {"message": "Evaluation deleted"}
