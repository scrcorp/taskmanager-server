"""앱 매장 라우터 — 현재 사용자의 매장 목록 API.

GET /my/stores — user_stores 테이블에서 사용자에게 배정된 매장 목록 반환.
"""

from typing import Annotated

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
        }
        for s in stores
    ]
