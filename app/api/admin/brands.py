"""관리자 브랜드 라우터 — 브랜드 CRUD 엔드포인트.

Admin Brand Router — CRUD endpoints for brand management.
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
    BrandCreate,
    BrandDetailResponse,
    BrandResponse,
    BrandUpdate,
)
from app.services.brand_service import brand_service

router: APIRouter = APIRouter()


@router.get("/", response_model=list[BrandResponse])
async def list_brands(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> list[BrandResponse]:
    """브랜드 목록을 조회합니다.

    List all brands in the current organization.
    """
    org_id: UUID = current_user.organization_id
    return await brand_service.list_brands(db, org_id)


@router.get("/{brand_id}", response_model=BrandDetailResponse)
async def get_brand(
    brand_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> BrandDetailResponse:
    """브랜드 상세 정보를 조회합니다 (근무조/직책 포함).

    Retrieve brand detail with shifts and positions.
    """
    org_id: UUID = current_user.organization_id
    return await brand_service.get_brand(db, brand_id, org_id)


@router.post("/", response_model=BrandResponse, status_code=201)
async def create_brand(
    data: BrandCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> BrandResponse:
    """새 브랜드를 생성합니다.

    Create a new brand in the current organization.
    """
    org_id: UUID = current_user.organization_id
    result: BrandResponse = await brand_service.create_brand(db, org_id, data)
    await db.commit()
    return result


@router.put("/{brand_id}", response_model=BrandResponse)
async def update_brand(
    brand_id: UUID,
    data: BrandUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> BrandResponse:
    """브랜드 정보를 수정합니다.

    Update an existing brand.
    """
    org_id: UUID = current_user.organization_id
    result: BrandResponse = await brand_service.update_brand(db, brand_id, org_id, data)
    await db.commit()
    return result


@router.delete("/{brand_id}", status_code=204)
async def delete_brand(
    brand_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> None:
    """브랜드를 삭제합니다.

    Delete a brand by its ID.
    """
    org_id: UUID = current_user.organization_id
    await brand_service.delete_brand(db, brand_id, org_id)
    await db.commit()
