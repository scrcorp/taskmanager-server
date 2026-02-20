"""관리자 근무조 라우터 — 매장 하위 근무조 CRUD 엔드포인트.

Admin Shift Router — CRUD endpoints for shifts under a store.
All endpoints are nested under /stores/{store_id}/shifts.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_supervisor
from app.database import get_db
from app.models.user import User
from app.schemas.work import ShiftCreate, ShiftResponse, ShiftUpdate
from app.services.shift_service import shift_service

router: APIRouter = APIRouter()


@router.get(
    "/stores/{store_id}/shifts",
    response_model=list[ShiftResponse],
)
async def list_shifts(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> list[ShiftResponse]:
    """매장에 속한 근무조 목록을 조회합니다.

    List all shifts belonging to a store.
    """
    org_id: UUID = current_user.organization_id
    return await shift_service.list_shifts(db, store_id, org_id)


@router.post(
    "/stores/{store_id}/shifts",
    response_model=ShiftResponse,
    status_code=201,
)
async def create_shift(
    store_id: UUID,
    data: ShiftCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> ShiftResponse:
    """새 근무조를 생성합니다.

    Create a new shift under a store.
    """
    org_id: UUID = current_user.organization_id
    result: ShiftResponse = await shift_service.create_shift(
        db, store_id, org_id, data
    )
    await db.commit()
    return result


@router.put(
    "/stores/{store_id}/shifts/{shift_id}",
    response_model=ShiftResponse,
)
async def update_shift(
    store_id: UUID,
    shift_id: UUID,
    data: ShiftUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> ShiftResponse:
    """근무조 정보를 수정합니다.

    Update an existing shift.
    """
    org_id: UUID = current_user.organization_id
    result: ShiftResponse = await shift_service.update_shift(
        db, shift_id, store_id, org_id, data
    )
    await db.commit()
    return result


@router.delete(
    "/stores/{store_id}/shifts/{shift_id}",
    status_code=204,
)
async def delete_shift(
    store_id: UUID,
    shift_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> None:
    """근무조를 삭제합니다.

    Delete a shift by its ID.
    """
    org_id: UUID = current_user.organization_id
    await shift_service.delete_shift(db, shift_id, store_id, org_id)
    await db.commit()
