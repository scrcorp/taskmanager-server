"""Super Owner 발급/양도 라우터.

Super Owner = 조직 관리자(priority=5). 조직당 1명, 매장 운영 비참여, 알림 비대상.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.user import User
from app.services.user_service import user_service

router: APIRouter = APIRouter()


class TransferSuperOwnerRequest(BaseModel):
    """Super Owner 양도 요청 — target Owner + 본인 확인용 현재 비밀번호."""

    target_user_id: UUID = Field(..., description="새 Super Owner 가 될 사용자 (현재 Owner)")
    current_password: str = Field(..., min_length=1, description="caller(현재 Super Owner) 본인 확인 비밀번호")


@router.get("/status")
async def get_super_owner_status(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("org:read"))],
) -> dict:
    """현재 조직의 Super Owner 계정 정보를 조회.

    조직 setup 시 자동 생성되므로 always exists. UI 표시용.
    Returns: { exists: bool, username: str|null }
    """
    return await user_service.get_super_owner_status(db, current_user.organization_id)


@router.post("/transfer")
async def transfer_super_owner(
    data: TransferSuperOwnerRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("super_owner:transfer"))],
) -> dict:
    """Super Owner 양도. caller(Super Owner) → Owner, target(Owner) → Super Owner.

    - target_user_id: 같은 조직의 활성 Owner 여야 함
    - current_password: caller 본인 확인용 비밀번호
    - 트랜잭션: role 스왑 + caller 매장 자동 배정 + target 매장 배정 제거
    """
    return await user_service.transfer_super_owner(
        db, current_user, data.target_user_id, data.current_password
    )
