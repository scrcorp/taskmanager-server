"""관리자 직책 라우터 — 매장 하위 직책 CRUD 엔드포인트.

Admin Position Router — CRUD endpoints for positions under a store.
All endpoints are nested under /stores/{store_id}/positions.

Permission Matrix (역할별 권한 설계):
    - Position 생성/수정/삭제: Owner + GM (담당 매장)
    - Position 목록 조회: Owner + GM + SV (소속 매장)
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.work import PositionCreate, PositionResponse, PositionUpdate
from app.services.position_service import position_service

router: APIRouter = APIRouter()


@router.get(
    "/stores/{store_id}/positions",
    response_model=list[PositionResponse],
)
async def list_positions(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:read"))],
) -> list[PositionResponse]:
    """매장에 속한 직책 목록을 조회합니다. 담당/소속 매장만 접근 가능.

    List all positions belonging to a store. Scoped to accessible stores.
    """
    await check_store_access(db, current_user, store_id)
    org_id: UUID = current_user.organization_id
    return await position_service.list_positions(db, store_id, org_id)


@router.post(
    "/stores/{store_id}/positions",
    response_model=PositionResponse,
    status_code=201,
)
async def create_position(
    store_id: UUID,
    data: PositionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:create"))],
) -> PositionResponse:
    """새 직책을 생성합니다. Owner + GM (담당 매장).

    Create a new position under a store. Owner + GM (assigned stores only).
    """
    await check_store_access(db, current_user, store_id)
    org_id: UUID = current_user.organization_id
    result: PositionResponse = await position_service.create_position(
        db, store_id, org_id, data
    )
    await db.commit()
    return result


@router.put(
    "/stores/{store_id}/positions/{position_id}",
    response_model=PositionResponse,
)
async def update_position(
    store_id: UUID,
    position_id: UUID,
    data: PositionUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> PositionResponse:
    """직책 정보를 수정합니다. Owner + GM (담당 매장).

    Update an existing position. Owner + GM (assigned stores only).
    """
    await check_store_access(db, current_user, store_id)
    org_id: UUID = current_user.organization_id
    result: PositionResponse = await position_service.update_position(
        db, position_id, store_id, org_id, data
    )
    await db.commit()
    return result


@router.delete(
    "/stores/{store_id}/positions/{position_id}",
    status_code=204,
)
async def delete_position(
    store_id: UUID,
    position_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:delete"))],
) -> None:
    """직책을 삭제합니다. Owner + GM (담당 매장).

    Delete a position by its ID. Owner + GM (assigned stores only).
    """
    await check_store_access(db, current_user, store_id)
    org_id: UUID = current_user.organization_id
    await position_service.delete_position(db, position_id, store_id, org_id)
    await db.commit()
