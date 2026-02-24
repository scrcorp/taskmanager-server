"""관리자 매장 라우터 — 매장 CRUD 엔드포인트.

Admin Store Router — CRUD endpoints for store management.
All endpoints are scoped to the current organization from JWT.

Permission Matrix (역할별 권한 설계):
    - 매장 등록/수정/삭제: Owner만
    - 매장 목록/상세 조회: Owner 전체, GM 담당 매장, SV 소속 매장
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    check_store_access,
    get_accessible_store_ids,
    require_owner,
    require_supervisor,
)
from app.database import get_db
from app.models.user import User
from app.schemas.organization import (
    StoreCreate,
    StoreDetailResponse,
    StoreResponse,
    StoreUpdate,
)
from app.services.store_service import store_service

router: APIRouter = APIRouter()


@router.get("", response_model=list[StoreResponse])
async def list_stores(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> list[StoreResponse]:
    """매장 목록을 조회합니다. Owner=전체, GM=담당 매장, SV=소속 매장.

    List stores scoped to user's accessible stores.
    """
    org_id: UUID = current_user.organization_id
    accessible = await get_accessible_store_ids(db, current_user)
    return await store_service.list_stores(db, org_id, accessible_store_ids=accessible)


@router.get("/{store_id}", response_model=StoreDetailResponse)
async def get_store(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> StoreDetailResponse:
    """매장 상세 정보를 조회합니다 (근무조/직책 포함). 담당 매장만 접근 가능.

    Retrieve store detail with shifts and positions. Scoped to accessible stores.
    """
    await check_store_access(db, current_user, store_id)
    org_id: UUID = current_user.organization_id
    return await store_service.get_store(db, store_id, org_id)


@router.post("", response_model=StoreResponse, status_code=201)
async def create_store(
    data: StoreCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_owner)],
) -> StoreResponse:
    """새 매장을 생성합니다. Owner만 가능.

    Create a new store in the current organization. Owner only.
    """
    org_id: UUID = current_user.organization_id
    result: StoreResponse = await store_service.create_store(db, org_id, data)
    await db.commit()
    return result


@router.put("/{store_id}", response_model=StoreResponse)
async def update_store(
    store_id: UUID,
    data: StoreUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_owner)],
) -> StoreResponse:
    """매장 정보를 수정합니다. Owner만 가능.

    Update an existing store. Owner only.
    """
    org_id: UUID = current_user.organization_id
    result: StoreResponse = await store_service.update_store(db, store_id, org_id, data)
    await db.commit()
    return result


@router.delete("/{store_id}", status_code=204)
async def delete_store(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_owner)],
) -> None:
    """매장을 삭제합니다. Owner만 가능.

    Delete a store by its ID. Owner only.
    """
    org_id: UUID = current_user.organization_id
    await store_service.delete_store(db, store_id, org_id)
    await db.commit()
