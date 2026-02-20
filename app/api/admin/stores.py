"""관리자 매장 라우터 — 매장 CRUD 엔드포인트.

Admin Store Router — CRUD endpoints for store management.
All endpoints are scoped to the current organization from JWT.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_supervisor
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
    """매장 목록을 조회합니다.

    List all stores in the current organization.
    """
    org_id: UUID = current_user.organization_id
    return await store_service.list_stores(db, org_id)


@router.get("/{store_id}", response_model=StoreDetailResponse)
async def get_store(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> StoreDetailResponse:
    """매장 상세 정보를 조회합니다 (근무조/직책 포함).

    Retrieve store detail with shifts and positions.
    """
    org_id: UUID = current_user.organization_id
    return await store_service.get_store(db, store_id, org_id)


@router.post("", response_model=StoreResponse, status_code=201)
async def create_store(
    data: StoreCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> StoreResponse:
    """새 매장을 생성합니다.

    Create a new store in the current organization.
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
    current_user: Annotated[User, Depends(require_supervisor)],
) -> StoreResponse:
    """매장 정보를 수정합니다.

    Update an existing store.
    """
    org_id: UUID = current_user.organization_id
    result: StoreResponse = await store_service.update_store(db, store_id, org_id, data)
    await db.commit()
    return result


@router.delete("/{store_id}", status_code=204)
async def delete_store(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> None:
    """매장을 삭제합니다.

    Delete a store by its ID.
    """
    org_id: UUID = current_user.organization_id
    await store_service.delete_store(db, store_id, org_id)
    await db.commit()
