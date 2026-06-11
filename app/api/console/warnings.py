"""관리자 경고 라우터 — Warning v1 API.

Admin Warning Router — `/api/v1/console/warnings`.

Routing order: 정적 경로(/warnable-users, /counts)를 동적 /{warning_id} 보다
먼저 등록해야 shadow 되지 않는다 (evaluations.py 패턴).

Permission Matrix (warnings:* 는 GM 이상에 기본 부여):
    - 조회(목록/상세/카운트): warnings:read
    - 발행/picker: warnings:create (방향 검증 — 발행자보다 낮은 권한만)
    - 수정/해결: warnings:update (소유권 — Owner 전체 / GM 본인)
    - 삭제(소프트): warnings:delete (소유권 동일)

Store scoping:
    - POST/PUT: check_store_access (불가 → 403)
    - GET /: store_id 필터를 accessible 과 intersect (불가 매장 → 빈 페이지)
    - GET /{id}: 경고의 store 접근 가능 / Owner / issuer 본인만 (아니면 404)
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
from app.schemas.warning import (
    WarnableUsersPage,
    WarningCountItem,
    WarningCreate,
    WarningResponse,
    WarningUpdate,
)
from app.services.warning_service import warning_service
from app.utils.exceptions import NotFoundError

router: APIRouter = APIRouter()


# ====================================================================
# 정적 경로 — /{warning_id} 보다 먼저 등록 (shadow 방지)
# ====================================================================


@router.get("/warnable-users", response_model=WarnableUsersPage)
async def list_warnable_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:create"))],
    store_id: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    page: int = 1,
    limit: int = 30,
) -> dict:
    """경고 대상 직원 picker — 방향 필터(엄격히 낮은 권한) + 매장 스코프 + 검색/페이지.

    store_id 가 주어지면 그 매장 접근 가능 여부를 먼저 검증(불가 → 403).
    각 후보는 stores[] 에 자신의 모든 매장을 포함한다(store dropdown 제한).
    """
    page = max(1, page)
    limit = max(1, min(limit, 100))
    store_uuid: UUID | None = UUID(store_id) if store_id else None
    if store_uuid is not None:
        await check_store_access(db, current_user, store_uuid)
    return await warning_service.list_warnable_users(
        db, current_user, store_id=store_uuid, q=q, page=page, limit=limit
    )


@router.get("/counts", response_model=list[WarningCountItem])
async def warning_counts(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:read"))],
) -> list[dict]:
    """직원별 경고 갯수 (total/active) — Staff 목록 Warnings 칼럼용.

    store-scope: Owner 전체, GM 관리매장 한정. 갯수 0인 직원은 결과에 없음.
    """
    accessible = await get_accessible_store_ids(db, current_user)
    store_ids = list(accessible) if accessible is not None else None
    return await warning_service.get_counts(
        db, current_user.organization_id, store_ids=store_ids
    )


# ====================================================================
# 경고 CRUD
# ====================================================================


@router.get("/", response_model=PaginatedResponse)
async def list_warnings(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:read"))],
    store_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    subject_user_id: Annotated[str | None, Query()] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """경고 목록 — org-scope, soft-delete 제외, created_at DESC.

    store_id 필터는 accessible 과 intersect (불가 매장 → 빈 페이지).
    subject_user_id 로 특정 직원 경고만 조회(Staff 상세 하단 이력).
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

    warnings, total = await warning_service.list_warnings(
        db,
        organization_id=current_user.organization_id,
        store_ids=store_ids,
        status=status,
        category=category,
        subject_user_id=UUID(subject_user_id) if subject_user_id else None,
        page=page,
        per_page=per_page,
    )
    items = [await warning_service.build_warning_response(db, w) for w in warnings]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/{warning_id}", response_model=WarningResponse)
async def get_warning(
    warning_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:read"))],
) -> dict:
    """경고 상세. org 밖/soft-deleted/부재 시 404.

    추가로 경고의 store 가 접근 불가 + Owner 아님 + issuer 본인 아님이면 404
    (cross-store 존재 누설 방지).
    """
    warning = await warning_service.get_warning(
        db, warning_id=warning_id, organization_id=current_user.organization_id
    )

    if not is_owner(current_user) and warning.issued_by_id != current_user.id:
        accessible = await get_accessible_store_ids(db, current_user)
        if (
            accessible is not None
            and warning.store_id is not None
            and warning.store_id not in accessible
        ):
            raise NotFoundError("Warning not found")

    return await warning_service.build_warning_response(db, warning, include_ordinal=True)


@router.post("/", response_model=WarningResponse, status_code=201)
async def create_warning(
    data: WarningCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:create"))],
) -> dict:
    """새 경고 발행. 매장 접근 검증 + 방향 검증(상위→하위) + subject-store 검증."""
    await check_store_access(db, current_user, UUID(data.store_id))
    warning = await warning_service.create_warning(
        db,
        organization_id=current_user.organization_id,
        issuer=current_user,
        data=data,
    )
    return await warning_service.build_warning_response(db, warning)


@router.put("/{warning_id}", response_model=WarningResponse)
async def update_warning(
    warning_id: UUID,
    data: WarningUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:update"))],
) -> dict:
    """경고 수정/해결. 소유권(Owner 전체 / GM 본인) + (store 변경 시)매장 재검증."""

    async def _check_store_access(store_id: UUID) -> None:
        await check_store_access(db, current_user, store_id)

    warning = await warning_service.update_warning(
        db,
        warning_id=warning_id,
        organization_id=current_user.organization_id,
        current_user=current_user,
        data=data,
        check_store_access=_check_store_access,
    )
    return await warning_service.build_warning_response(db, warning)


@router.delete("/{warning_id}", response_model=MessageResponse)
async def delete_warning(
    warning_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:delete"))],
) -> dict:
    """경고 소프트 삭제. 소유권 검증. 이미 삭제/부재면 404 (idempotent-safe)."""
    await warning_service.delete_warning(
        db,
        warning_id=warning_id,
        organization_id=current_user.organization_id,
        current_user=current_user,
    )
    return {"message": "Warning deleted"}
