"""관리자 시프트 프리셋 라우터 — Shift Preset CRUD 엔드포인트.

Admin Shift Preset Router — CRUD endpoints for shift presets.
Presets are scoped under a store: /stores/{store_id}/shift-presets
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, require_gm, require_supervisor
from app.database import get_db
from app.models.user import User
from app.schemas.shift_preset import ShiftPresetCreate, ShiftPresetResponse, ShiftPresetUpdate
from app.services.shift_preset_service import shift_preset_service

router: APIRouter = APIRouter()


@router.get("/stores/{store_id}/shift-presets", response_model=list[ShiftPresetResponse])
async def list_shift_presets(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> list[ShiftPresetResponse]:
    await check_store_access(db, current_user, store_id)
    return await shift_preset_service.list_presets(db, store_id)


@router.post("/stores/{store_id}/shift-presets", response_model=ShiftPresetResponse, status_code=201)
async def create_shift_preset(
    store_id: UUID,
    data: ShiftPresetCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> ShiftPresetResponse:
    await check_store_access(db, current_user, store_id)
    result = await shift_preset_service.create_preset(db, current_user.organization_id, store_id, data)
    await db.commit()
    return result


@router.put("/shift-presets/{preset_id}", response_model=ShiftPresetResponse)
async def update_shift_preset(
    preset_id: UUID,
    data: ShiftPresetUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> ShiftPresetResponse:
    result = await shift_preset_service.update_preset(db, preset_id, current_user.organization_id, data)
    await db.commit()
    return result


@router.delete("/shift-presets/{preset_id}", status_code=204)
async def delete_shift_preset(
    preset_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> None:
    await shift_preset_service.delete_preset(db, preset_id, current_user.organization_id)
    await db.commit()
