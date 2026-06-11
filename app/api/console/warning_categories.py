"""관리자 경고 카테고리 라우터 — Warning category API (v1.1).

Admin Warning Category Router — `/api/v1/console/warning-categories`.

Permission:
    - 조회(GET /): warnings:read — 경고 발행/조회 누구나(폼 picker + 관리화면 공용).
      응답에 is_hidden/is_system 포함 → 프론트가 picker용(비숨김)으로 필터.
    - 추가/이름변경/숨김/삭제: **Owner only** (org 설정이므로). is_owner 강제.

org-scope: current_user.organization_id 로 격리.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.permissions import is_owner
from app.database import get_db
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.warning_category import (
    WarningCategoryCreate,
    WarningCategoryResponse,
    WarningCategoryUpdate,
)
from app.services.warning_category_service import warning_category_service

router: APIRouter = APIRouter()


def _assert_owner(current_user: User) -> None:
    """카테고리 관리(추가/수정/삭제)는 Owner(super_owner 포함)만."""
    if not is_owner(current_user):
        raise HTTPException(
            status_code=403,
            detail="Only an Owner can manage warning categories",
        )


@router.get("", response_model=list[WarningCategoryResponse])
async def list_categories(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:read"))],
) -> list[dict]:
    """org 카테고리 목록 (비삭제, 숨김 포함). sort_order 순(other 맨 끝).

    프론트: 폼 picker 는 is_hidden=False 만, 관리화면은 전체 + Hidden 섹션.
    """
    categories = await warning_category_service.list_categories(
        db, current_user.organization_id, include_hidden=True
    )
    return [warning_category_service.to_response(c) for c in categories]


@router.post("", response_model=WarningCategoryResponse, status_code=201)
async def create_category(
    data: WarningCategoryCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:read"))],
) -> dict:
    """카테고리 추가 (Owner only). 같은 code 가 삭제돼 있으면 revive."""
    _assert_owner(current_user)
    category = await warning_category_service.create_category(
        db, current_user.organization_id, data.label
    )
    return warning_category_service.to_response(category)


@router.patch("/{category_id}", response_model=WarningCategoryResponse)
async def update_category(
    category_id: str,
    data: WarningCategoryUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:read"))],
) -> dict:
    """카테고리 이름 변경 / 숨김 토글 (Owner only). system(other) 숨김 불가."""
    _assert_owner(current_user)
    category = await warning_category_service.update_category(
        db,
        current_user.organization_id,
        UUID(category_id),
        label=data.label,
        is_hidden=data.is_hidden,
    )
    return warning_category_service.to_response(category)


@router.delete("/{category_id}", response_model=MessageResponse)
async def delete_category(
    category_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:read"))],
) -> dict:
    """카테고리 soft delete (Owner only). system(other) 삭제 불가. (재추가 시 revive)"""
    _assert_owner(current_user)
    await warning_category_service.delete_category(
        db, current_user.organization_id, UUID(category_id)
    )
    return {"message": "Category deleted"}
