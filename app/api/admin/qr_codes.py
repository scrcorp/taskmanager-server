"""관리자 QR 코드 라우터 — 매장 QR 코드 관리 API.

Admin QR Code Router — API endpoints for store QR code management.
Provides QR code generation, retrieval, and regeneration.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_gm
from app.database import get_db
from app.models.user import User
from app.schemas.common import QRCodeResponse
from app.services.attendance_service import attendance_service
from app.utils.exceptions import NotFoundError

router: APIRouter = APIRouter()


@router.post("/stores/{store_id}/qr-codes", response_model=QRCodeResponse, status_code=201)
async def create_qr_code(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> dict:
    """매장의 QR 코드를 생성합니다 (GM+ 전용). 기존 활성 QR은 비활성화됩니다.

    Generate a new QR code for a store (GM+ only).
    Deactivates any existing active QR codes for the store.

    Args:
        store_id: 매장 UUID (Store UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 GM 이상 사용자 (Authenticated GM+ user)

    Returns:
        dict: 생성된 QR 코드 (Created QR code)
    """
    qr = await attendance_service.create_qr_code(
        db,
        store_id=store_id,
        created_by=current_user.id,
    )
    await db.commit()

    return await attendance_service.build_qr_response(db, qr)


@router.get("/stores/{store_id}/qr-codes", response_model=QRCodeResponse)
async def get_store_qr_code(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> dict:
    """매장의 활성 QR 코드를 조회합니다 (GM+ 전용).

    Get the active QR code for a store (GM+ only).

    Args:
        store_id: 매장 UUID (Store UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 GM 이상 사용자 (Authenticated GM+ user)

    Returns:
        dict: 활성 QR 코드 (Active QR code)

    Raises:
        NotFoundError: 활성 QR 코드가 없을 때 (No active QR code found)
    """
    qr = await attendance_service.get_store_qr(db, store_id)
    if qr is None:
        raise NotFoundError("활성 QR 코드가 없습니다 (No active QR code found for this store)")

    return await attendance_service.build_qr_response(db, qr)


@router.post("/qr-codes/{qr_id}/regenerate", response_model=QRCodeResponse)
async def regenerate_qr_code(
    qr_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_gm)],
) -> dict:
    """QR 코드를 재생성합니다 (GM+ 전용). 기존 QR은 비활성화되고 새 QR이 생성됩니다.

    Regenerate a QR code (GM+ only). Old QR is deactivated and a new one is created.

    Args:
        qr_id: 기존 QR 코드 UUID (Existing QR code UUID)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 GM 이상 사용자 (Authenticated GM+ user)

    Returns:
        dict: 새로 생성된 QR 코드 (Newly created QR code)
    """
    qr = await attendance_service.regenerate_qr_code(
        db,
        qr_id=qr_id,
        created_by=current_user.id,
    )
    await db.commit()

    return await attendance_service.build_qr_response(db, qr)
