"""Admin Attendance Devices 라우터 — 기기 목록/rename/revoke + access code 조회/rotate.

Mounted under /api/v1/admin.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.access_code import get_code, rotate_code
from app.database import get_db
from app.models.organization import Store
from app.models.user import User
from app.schemas.attendance_device import (
    AdminAccessCodeResponse,
    AdminDeviceRenameRequest,
    AdminDeviceResponse,
)
from app.services.attendance_device_service import attendance_device_service

router: APIRouter = APIRouter()


async def _build_device_response(db: AsyncSession, device) -> AdminDeviceResponse:
    store_name: str | None = None
    if device.store_id is not None:
        result = await db.execute(select(Store).where(Store.id == device.store_id))
        store = result.scalar_one_or_none()
        store_name = store.name if store else None
    return AdminDeviceResponse(
        id=device.id,
        organization_id=device.organization_id,
        store_id=device.store_id,
        store_name=store_name,
        device_name=device.device_name,
        fingerprint=device.fingerprint,
        registered_at=device.registered_at,
        last_seen_at=device.last_seen_at,
        revoked_at=device.revoked_at,
    )


@router.get("/attendance-devices", response_model=list[AdminDeviceResponse])
async def list_devices(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("attendance_devices:read"))],
    include_revoked: bool = False,
) -> list[AdminDeviceResponse]:
    devices = await attendance_device_service.list_for_org(
        db, current_user.organization_id, include_revoked=include_revoked
    )
    return [await _build_device_response(db, d) for d in devices]


@router.patch("/attendance-devices/{device_id}", response_model=AdminDeviceResponse)
async def rename_device(
    device_id: UUID,
    data: AdminDeviceRenameRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("attendance_devices:update"))],
) -> AdminDeviceResponse:
    device = await attendance_device_service.get_admin(
        db, current_user.organization_id, device_id
    )
    await attendance_device_service.rename(db, device, data.device_name)
    await db.commit()
    return await _build_device_response(db, device)


@router.delete("/attendance-devices/{device_id}", status_code=204)
async def revoke_device(
    device_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("attendance_devices:update"))],
) -> None:
    device = await attendance_device_service.get_admin(
        db, current_user.organization_id, device_id
    )
    await attendance_device_service.revoke(db, device)
    await db.commit()


# ── Access Code 관리 ──────────────────────────────────────


@router.get("/access-codes/{service_key}", response_model=AdminAccessCodeResponse)
async def get_access_code(
    service_key: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("attendance_devices:read"))],
) -> AdminAccessCodeResponse:
    record = await get_code(db, service_key)
    if record is None:
        from fastapi import HTTPException, status as http_status
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Access code not found")
    return AdminAccessCodeResponse(
        service_key=record.service_key,
        code=record.code,
        source=record.source,
        rotated_at=record.rotated_at,
        created_at=record.created_at,
    )


@router.post("/access-codes/{service_key}/rotate", response_model=AdminAccessCodeResponse)
async def rotate_access_code(
    service_key: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("attendance_devices:update"))],
) -> AdminAccessCodeResponse:
    record = await rotate_code(db, service_key)
    await db.commit()
    return AdminAccessCodeResponse(
        service_key=record.service_key,
        code=record.code,
        source=record.source,
        rotated_at=record.rotated_at,
        created_at=record.created_at,
    )
