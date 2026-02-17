"""관리자 사용자 라우터 — 사용자 CRUD 및 브랜드 배정 엔드포인트.

Admin User Router — CRUD and brand assignment endpoints for user management.
Provides user listing with filters, detail retrieval, creation, update,
activation toggle, and user-brand association management.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_manager, require_supervisor
from app.database import get_db
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.organization import BrandResponse
from app.schemas.user import (
    UserCreate,
    UserListResponse,
    UserResponse,
    UserUpdate,
)
from app.services.user_service import user_service

router: APIRouter = APIRouter()


@router.get("/", response_model=list[UserListResponse])
async def list_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
    brand_id: Annotated[UUID | None, Query(description="브랜드 ID 필터")] = None,
    role_id: Annotated[UUID | None, Query(description="역할 ID 필터")] = None,
    is_active: Annotated[bool | None, Query(description="활성 상태 필터")] = None,
) -> list[UserListResponse]:
    """사용자 목록을 필터 조건으로 조회합니다.

    List users with optional filters (brand_id, role_id, is_active).
    """
    org_id: UUID = current_user.organization_id
    filters: dict[str, UUID | bool | None] = {
        "brand_id": brand_id,
        "role_id": role_id,
        "is_active": is_active,
    }
    return await user_service.list_users(db, org_id, filters)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> UserResponse:
    """사용자 상세 정보를 조회합니다.

    Retrieve user detail with role information.
    """
    org_id: UUID = current_user.organization_id
    return await user_service.get_user(db, user_id, org_id)


@router.post("/", response_model=UserResponse, status_code=201)
async def create_user(
    data: UserCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_manager)],
) -> UserResponse:
    """새 사용자를 생성합니다.

    Create a new user in the current organization.
    """
    org_id: UUID = current_user.organization_id
    result: UserResponse = await user_service.create_user(db, org_id, data)
    await db.commit()
    return result


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: UUID,
    data: UserUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_manager)],
) -> UserResponse:
    """사용자 정보를 수정합니다.

    Update an existing user's information.
    """
    org_id: UUID = current_user.organization_id
    result: UserResponse = await user_service.update_user(db, user_id, org_id, data)
    await db.commit()
    return result


@router.patch("/{user_id}/active", response_model=UserResponse)
async def toggle_user_active(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_manager)],
) -> UserResponse:
    """사용자 활성/비활성 상태를 토글합니다.

    Toggle a user's active/inactive status.
    """
    org_id: UUID = current_user.organization_id
    result: UserResponse = await user_service.toggle_active(db, user_id, org_id)
    await db.commit()
    return result


@router.delete("/{user_id}", response_model=MessageResponse)
async def delete_user(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_manager)],
) -> dict[str, str]:
    """사용자를 삭제합니다 (소프트 삭제: is_active=False 처리).

    Delete a user (soft-delete: sets is_active=False and clears brand assignments).
    Only managers and above can delete users.

    Args:
        user_id: 삭제할 사용자 UUID (User UUID to delete)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 관리자 사용자 (Authenticated admin user)

    Returns:
        dict: 삭제 결과 메시지 (Deletion result message)
    """
    org_id: UUID = current_user.organization_id
    await user_service.delete_user(db, user_id, org_id)
    await db.commit()
    return {"message": "사용자가 삭제되었습니다 (User deleted successfully)"}


@router.get("/{user_id}/brands", response_model=list[BrandResponse])
async def get_user_brands(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> list[BrandResponse]:
    """사용자에게 배정된 브랜드 목록을 조회합니다.

    Retrieve all brands assigned to a user.
    """
    org_id: UUID = current_user.organization_id
    return await user_service.get_user_brands(db, user_id, org_id)


@router.post("/{user_id}/brands/{brand_id}", status_code=201)
async def add_user_brand(
    user_id: UUID,
    brand_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_manager)],
) -> dict[str, str]:
    """사용자에게 브랜드를 배정합니다.

    Assign a brand to a user.
    """
    org_id: UUID = current_user.organization_id
    await user_service.add_user_brand(db, user_id, brand_id, org_id)
    await db.commit()
    return {"message": "Brand assigned successfully"}


@router.delete("/{user_id}/brands/{brand_id}", status_code=204)
async def remove_user_brand(
    user_id: UUID,
    brand_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_manager)],
) -> None:
    """사용자에게서 브랜드 배정을 해제합니다.

    Remove a brand assignment from a user.
    """
    org_id: UUID = current_user.organization_id
    await user_service.remove_user_brand(db, user_id, brand_id, org_id)
    await db.commit()
