"""Admin Attendance Devices 라우터 — 기기 목록/rename/revoke + access code 조회/rotate.

Mounted under /api/v1/console.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.access_code import ensure_code, get_code, rotate_code, set_code
from app.database import get_db
from app.models.access_code_audit import (
    ACCESS_CODE_AUDIT_MANUAL_SET,
    ACCESS_CODE_AUDIT_ROTATE,
)
from app.models.organization import Store
from app.models.user import User
from app.repositories.access_code_audit_repository import access_code_audit_repository
from app.schemas.attendance_device import (
    AdminAccessCodeResponse,
    AdminAccessCodeSetRequest,
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
    )


@router.get("/attendance-devices", response_model=list[AdminDeviceResponse])
async def list_devices(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("attendance_devices:read"))],
) -> list[AdminDeviceResponse]:
    devices = await attendance_device_service.list_for_org(
        db, current_user.organization_id
    )
    return [await _build_device_response(db, d) for d in devices]


@router.patch("/attendance-devices/{device_id}", response_model=AdminDeviceResponse)
async def rename_device(
    device_id: UUID,
    data: AdminDeviceRenameRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("attendance_devices:update"))],
) -> AdminDeviceResponse:
    device = await attendance_device_service.get_manage(
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
    device = await attendance_device_service.get_manage(
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
    # 자기 조직 코드만 조회. 없으면 자동 생성해 항상 코드를 노출(운영자가 기기 등록에 바로 사용).
    record = await get_code(db, service_key, current_user.organization_id)
    if record is None:
        record = await ensure_code(db, service_key, current_user.organization_id)
        await db.commit()
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
    # 자기 조직 코드만 rotate — 다른 조직 코드에 영향 없음.
    record = await rotate_code(db, service_key, current_user.organization_id)
    await access_code_audit_repository.record(
        db,
        organization_id=current_user.organization_id,
        service_key=service_key,
        action=ACCESS_CODE_AUDIT_ROTATE,
        actor_user_id=current_user.id,
        meta={"code_length": len(record.code)},
    )
    await db.commit()
    return AdminAccessCodeResponse(
        service_key=record.service_key,
        code=record.code,
        source=record.source,
        rotated_at=record.rotated_at,
        created_at=record.created_at,
    )


@router.put("/access-codes/{service_key}", response_model=AdminAccessCodeResponse)
async def set_access_code(
    service_key: str,
    data: AdminAccessCodeSetRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("attendance_devices:update"))],
) -> AdminAccessCodeResponse:
    # 운영자 지정 코드 — 자기 조직 코드만. 타 org 점유 코드는 set_code 가 409.
    record = await set_code(db, service_key, current_user.organization_id, data.code)
    await access_code_audit_repository.record(
        db,
        organization_id=current_user.organization_id,
        service_key=service_key,
        action=ACCESS_CODE_AUDIT_MANUAL_SET,
        actor_user_id=current_user.id,
        meta={"code_length": len(record.code)},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        # 동시 요청이 set_code 의 사전 검사를 함께 통과한 경우 —
        # uq_access_code_service_code 제약이 최종 방어. 같은 409 로 변환.
        await db.rollback()
        if "uq_access_code_service_code" in str(exc.orig):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "access_code_taken",
                    "message": "This code is already used by another organization. Choose a different code.",
                },
            ) from exc
        raise
    return AdminAccessCodeResponse(
        service_key=record.service_key,
        code=record.code,
        source=record.source,
        rotated_at=record.rotated_at,
        created_at=record.created_at,
    )
