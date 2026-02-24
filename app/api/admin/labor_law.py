"""관리자 노동법 설정 라우터 — Labor Law Setting 엔드포인트.

Admin Labor Law Router — GET/PUT endpoints for per-store labor law settings.
Nested under stores: /stores/{store_id}/labor-law
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, require_gm, require_supervisor
from app.database import get_db
from app.models.user import User
from app.schemas.labor_law import LaborLawSettingResponse, LaborLawSettingUpdate
from app.services.labor_law_service import labor_law_service

router: APIRouter = APIRouter()


@router.get("/stores/{store_id}/labor-law", response_model=LaborLawSettingResponse | None)
async def get_labor_law(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> LaborLawSettingResponse | None:
    await check_store_access(db, current_user, store_id)
    return await labor_law_service.get_setting(db, store_id, current_user.organization_id)


@router.put("/stores/{store_id}/labor-law", response_model=LaborLawSettingResponse)
async def upsert_labor_law(
    store_id: UUID,
    data: LaborLawSettingUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> LaborLawSettingResponse:
    await check_store_access(db, current_user, store_id)
    result = await labor_law_service.upsert_setting(db, store_id, current_user.organization_id, data)
    await db.commit()
    return result
