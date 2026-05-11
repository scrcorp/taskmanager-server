"""관리자 휴게 규칙 라우터."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.schedule import BreakRuleResponse, BreakRuleUpsert
from app.services.break_rule_service import break_rule_service

router: APIRouter = APIRouter()


@router.get(
    "/stores/{store_id}/break-rules", response_model=BreakRuleResponse | None
)
async def get_break_rules(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:read"))],
) -> BreakRuleResponse | None:
    """매장의 휴게 규칙을 조회합니다."""
    await check_store_access(db, current_user, store_id)
    return await break_rule_service.get_break_rule(
        db, store_id, current_user.organization_id
    )


@router.put("/stores/{store_id}/break-rules", response_model=BreakRuleResponse)
async def upsert_break_rules(
    store_id: UUID,
    data: BreakRuleUpsert,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> BreakRuleResponse:
    """매장의 휴게 규칙을 생성/수정합니다."""
    await check_store_access(db, current_user, store_id)
    return await break_rule_service.upsert_break_rule(
        db, store_id, current_user.organization_id, data
    )
