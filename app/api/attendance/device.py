"""Attendance device 등록 / 정보 / 매장 할당 / 해제 라우터.

`/api/v1/attendance` 하위에 mount.
"""

from datetime import datetime as _dt, timezone as _tz
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_attendance_device
from app.core.access_code import verify_code
from app.database import get_db
from app.models.attendance_device import AttendanceDevice
from app.models.organization import Organization, Store
from app.schemas.attendance_device import (
    AssignStoreRequest,
    AttendanceStoreOption,
    DeviceMeResponse,
    RegisterRequest,
    RegisterResponse,
)
from app.services.attendance_device_service import attendance_device_service
from app.utils.timezone import get_store_day_config, get_work_date


router: APIRouter = APIRouter()

ACCESS_CODE_SERVICE_KEY = "attendance"


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register_device(
    data: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RegisterResponse:
    """Access code 를 검증하고 새 기기 토큰을 발급."""
    # access_code 는 service_key 당 1개이며, organization 을 식별하지 않는다.
    # 현재 단일 조직 배포를 가정 — 없으면 400.
    if not await verify_code(db, ACCESS_CODE_SERVICE_KEY, data.access_code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access code",
        )
    # 현재 시스템은 single-org 운영을 가정 (조직 1개 또는 대표 조직 1개).
    org_result = await db.execute(select(Organization).order_by(Organization.created_at).limit(1))
    organization = org_result.scalar_one_or_none()
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No organization configured",
        )
    device, token = await attendance_device_service.register(
        db, organization_id=organization.id, fingerprint=data.fingerprint
    )
    await db.commit()
    return RegisterResponse(
        token=token,
        device_id=device.id,
        device_name=device.device_name,
        store_id=device.store_id,
    )


@router.get("/me", response_model=DeviceMeResponse)
async def get_me(
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeviceMeResponse:
    """현재 토큰의 기기 정보 — store tz 기준 work_date 포함."""
    store_name: str | None = None
    store_tz: str | None = None
    work_date_str: str | None = None
    offset_minutes: int | None = None
    if device.store_id is not None:
        store_result = await db.execute(select(Store).where(Store.id == device.store_id))
        store = store_result.scalar_one_or_none()
        store_name = store.name if store else None
        tz, day_start = await get_store_day_config(db, device.store_id)
        store_tz = tz
        now_utc = _dt.now(_tz.utc)
        wd = get_work_date(tz, day_start, now_utc)
        work_date_str = wd.isoformat()
        # 현재 시각의 store tz UTC offset (DST 반영). 분 단위.
        try:
            local = now_utc.astimezone(ZoneInfo(tz))
            off = local.utcoffset()
            if off is not None:
                offset_minutes = int(off.total_seconds() // 60)
        except Exception:
            offset_minutes = None
    return DeviceMeResponse(
        device_id=device.id,
        device_name=device.device_name,
        organization_id=device.organization_id,
        store_id=device.store_id,
        store_name=store_name,
        store_timezone=store_tz,
        store_timezone_offset_minutes=offset_minutes,
        work_date=work_date_str,
        registered_at=device.registered_at,
        last_seen_at=device.last_seen_at,
    )


@router.put("/store", response_model=DeviceMeResponse)
async def assign_store(
    data: AssignStoreRequest,
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeviceMeResponse:
    """매장 선택/변경 — 최초 setup 또는 Change Store 흐름."""
    await attendance_device_service.assign_store(db, device, data.store_id)
    await db.commit()
    store_result = await db.execute(select(Store).where(Store.id == device.store_id))
    store = store_result.scalar_one_or_none()

    store_tz: str | None = None
    offset_minutes: int | None = None
    work_date_str: str | None = None
    if device.store_id is not None:
        tz, day_start = await get_store_day_config(db, device.store_id)
        store_tz = tz
        now_utc = _dt.now(_tz.utc)
        work_date_str = get_work_date(tz, day_start, now_utc).isoformat()
        try:
            off = now_utc.astimezone(ZoneInfo(tz)).utcoffset()
            if off is not None:
                offset_minutes = int(off.total_seconds() // 60)
        except Exception:
            offset_minutes = None

    return DeviceMeResponse(
        device_id=device.id,
        device_name=device.device_name,
        organization_id=device.organization_id,
        store_id=device.store_id,
        store_name=store.name if store else None,
        store_timezone=store_tz,
        store_timezone_offset_minutes=offset_minutes,
        work_date=work_date_str,
        registered_at=device.registered_at,
        last_seen_at=device.last_seen_at,
    )


@router.get("/stores", response_model=list[AttendanceStoreOption])
async def list_stores(
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AttendanceStoreOption]:
    """Device token 으로 조직 내 매장 후보 조회 (store select 화면용).

    기기는 JWT 가 없어 일반 store list API 를 호출할 수 없다. 등록된 organization
    내의 모든 매장 (soft-deleted 제외) 을 최소 정보만 반환.
    """
    result = await db.execute(
        select(Store)
        .where(
            Store.organization_id == device.organization_id,
            Store.deleted_at.is_(None),
        )
        .order_by(Store.name)
    )
    stores = result.scalars().all()
    return [AttendanceStoreOption(id=s.id, name=s.name) for s in stores]


@router.delete("/me", status_code=204)
async def unregister_device(
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """기기 자체 해제."""
    await attendance_device_service.revoke(db, device)
    await db.commit()
