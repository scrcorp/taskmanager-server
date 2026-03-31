"""앱 매장 라우터 — 현재 사용자의 매장 목록 API.

GET /my/stores — user_stores 테이블에서 사용자에게 배정된 매장 목록 반환.
GET /my/stores/{store_id}/work-date — 매장의 현재 work_date 반환.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.organization import Store
from app.models.user import User
from app.models.user_store import UserStore

router: APIRouter = APIRouter()


@router.get("")
async def get_my_stores(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[dict]:
    """현재 사용자에게 배정된 매장 목록을 반환합니다."""
    query = (
        select(Store)
        .join(UserStore, UserStore.store_id == Store.id)
        .where(
            UserStore.user_id == current_user.id,
            Store.is_active.is_(True),
        )
        .order_by(Store.name)
    )
    result = await db.execute(query)
    stores = result.scalars().all()
    return [
        {
            "id": str(s.id),
            "name": s.name,
            "address": s.address,
            "is_active": s.is_active,
            "timezone": s.timezone,
            "day_start_time": s.day_start_time,
        }
        for s in stores
    ]


@router.get("/{store_id}/work-date")
async def get_store_work_date(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """매장의 현재 work_date를 경계 시각 기준으로 반환합니다."""
    from app.utils.timezone import get_store_day_config, get_work_date
    store_tz, day_start = await get_store_day_config(db, store_id)
    work_date: date = get_work_date(store_tz, day_start)
    return {
        "store_id": str(store_id),
        "work_date": str(work_date),
        "timezone": store_tz,
        "day_start_time": day_start,
    }
