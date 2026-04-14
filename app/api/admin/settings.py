"""관리자 Settings Registry / Org / Store / Staff settings API."""

from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.permissions import is_owner
from app.database import get_db
from app.models.settings import OrgSetting, SettingsRegistry, StaffSetting, StoreSetting
from app.models.user import User
from app.schemas.settings import (
    OrgSettingResponse, OrgSettingUpsert,
    ResolvedSettingResponse,
    SettingsRegistryResponse, SettingsRegistryUpsert,
    StaffSettingResponse, StaffSettingUpsert,
    StoreSettingResponse, StoreSettingUpsert,
)
from app.utils.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.utils.settings_resolver import SettingNotRegisteredError, resolve_setting

router: APIRouter = APIRouter()


# ─── Registry ──────────────────────────────────────

@router.get("/registry", response_model=list[SettingsRegistryResponse])
async def list_registry(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("org:read"))],
    category: Annotated[str | None, Query()] = None,
) -> list[SettingsRegistryResponse]:
    """Settings registry 메타 조회 (전체 또는 카테고리 필터)."""
    stmt = select(SettingsRegistry)
    if category:
        stmt = stmt.where(SettingsRegistry.category == category)
    stmt = stmt.order_by(SettingsRegistry.category, SettingsRegistry.key)
    result = await db.execute(stmt)
    items = list(result.scalars().all())
    return [
        SettingsRegistryResponse(
            key=r.key, label=r.label, description=r.description,
            value_type=r.value_type, levels=r.levels,
            default_priority=r.default_priority, default_value=r.default_value,
            validation_schema=r.validation_schema, category=r.category,
            created_at=r.created_at, updated_at=r.updated_at,
        )
        for r in items
    ]


@router.put("/registry", response_model=SettingsRegistryResponse)
async def upsert_registry(
    data: SettingsRegistryUpsert,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("org:update"))],
) -> SettingsRegistryResponse:
    """Settings registry 메타 등록/수정. Owner 권한 권장."""
    if not is_owner(current_user):
        raise ForbiddenError("Owner only")
    existing = await db.scalar(select(SettingsRegistry).where(SettingsRegistry.key == data.key))
    if existing is None:
        existing = SettingsRegistry(
            key=data.key, label=data.label, description=data.description,
            value_type=data.value_type, levels=data.levels,
            default_priority=data.default_priority, default_value=data.default_value,
            validation_schema=data.validation_schema, category=data.category,
        )
        db.add(existing)
    else:
        existing.label = data.label
        existing.description = data.description
        existing.value_type = data.value_type
        existing.levels = data.levels
        existing.default_priority = data.default_priority
        existing.default_value = data.default_value
        existing.validation_schema = data.validation_schema
        existing.category = data.category
    await db.commit()
    await db.refresh(existing)
    return SettingsRegistryResponse(
        key=existing.key, label=existing.label, description=existing.description,
        value_type=existing.value_type, levels=existing.levels,
        default_priority=existing.default_priority, default_value=existing.default_value,
        validation_schema=existing.validation_schema, category=existing.category,
        created_at=existing.created_at, updated_at=existing.updated_at,
    )


# ─── Org Settings ──────────────────────────────────

@router.get("/org", response_model=list[OrgSettingResponse])
async def list_org_settings(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("org:read"))],
) -> list[OrgSettingResponse]:
    """현재 조직의 모든 설정 override 조회."""
    result = await db.execute(
        select(OrgSetting).where(OrgSetting.organization_id == current_user.organization_id)
    )
    items = list(result.scalars().all())
    return [
        OrgSettingResponse(
            id=str(o.id), organization_id=str(o.organization_id),
            key=o.key, value=o.value, force_locked=o.force_locked,
            updated_by=str(o.updated_by) if o.updated_by else None,
            updated_at=o.updated_at,
        )
        for o in items
    ]


@router.put("/org", response_model=OrgSettingResponse)
async def upsert_org_setting(
    data: OrgSettingUpsert,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("org:update"))],
) -> OrgSettingResponse:
    """조직 설정 upsert. registry 키 검증."""
    registry = await db.scalar(select(SettingsRegistry).where(SettingsRegistry.key == data.key))
    if registry is None:
        raise BadRequestError(f"Setting key '{data.key}' is not registered")
    if "org" not in (registry.levels or []):
        raise BadRequestError(f"Setting '{data.key}' does not allow org-level override")

    existing = await db.scalar(
        select(OrgSetting).where(
            OrgSetting.organization_id == current_user.organization_id,
            OrgSetting.key == data.key,
        )
    )
    if existing is None:
        existing = OrgSetting(
            organization_id=current_user.organization_id,
            key=data.key,
            value=data.value,
            force_locked=data.force_locked,
            updated_by=current_user.id,
        )
        db.add(existing)
    else:
        existing.value = data.value
        existing.force_locked = data.force_locked
        existing.updated_by = current_user.id
        existing.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(existing)
    return OrgSettingResponse(
        id=str(existing.id), organization_id=str(existing.organization_id),
        key=existing.key, value=existing.value, force_locked=existing.force_locked,
        updated_by=str(existing.updated_by) if existing.updated_by else None,
        updated_at=existing.updated_at,
    )


@router.delete("/org/{key}", status_code=204)
async def delete_org_setting(
    key: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("org:update"))],
) -> None:
    """조직 설정 override 제거 (default로 복원)."""
    existing = await db.scalar(
        select(OrgSetting).where(
            OrgSetting.organization_id == current_user.organization_id,
            OrgSetting.key == key,
        )
    )
    if existing is None:
        return
    await db.delete(existing)
    await db.commit()


# ─── Store Settings ────────────────────────────────

@router.get("/stores/{store_id}", response_model=list[StoreSettingResponse])
async def list_store_settings(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:read"))],
) -> list[StoreSettingResponse]:
    """매장 설정 override 조회."""
    result = await db.execute(
        select(StoreSetting).where(StoreSetting.store_id == store_id)
    )
    items = list(result.scalars().all())
    return [
        StoreSettingResponse(
            id=str(s.id), store_id=str(s.store_id),
            key=s.key, value=s.value,
            updated_by=str(s.updated_by) if s.updated_by else None,
            updated_at=s.updated_at,
        )
        for s in items
    ]


@router.put("/stores/{store_id}", response_model=StoreSettingResponse)
async def upsert_store_setting(
    store_id: UUID,
    data: StoreSettingUpsert,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> StoreSettingResponse:
    """매장 설정 upsert. registry + force_locked 체크."""
    registry = await db.scalar(select(SettingsRegistry).where(SettingsRegistry.key == data.key))
    if registry is None:
        raise BadRequestError(f"Setting key '{data.key}' is not registered")
    if "store" not in (registry.levels or []):
        raise BadRequestError(f"Setting '{data.key}' does not allow store-level override")

    # Org level force_locked 체크
    org_setting = await db.scalar(
        select(OrgSetting).where(
            OrgSetting.organization_id == current_user.organization_id,
            OrgSetting.key == data.key,
        )
    )
    if org_setting and org_setting.force_locked:
        raise ForbiddenError(f"Setting '{data.key}' is locked at organization level")

    existing = await db.scalar(
        select(StoreSetting).where(
            StoreSetting.store_id == store_id,
            StoreSetting.key == data.key,
        )
    )
    if existing is None:
        existing = StoreSetting(
            store_id=store_id, key=data.key, value=data.value,
            updated_by=current_user.id,
        )
        db.add(existing)
    else:
        existing.value = data.value
        existing.updated_by = current_user.id
        existing.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(existing)
    return StoreSettingResponse(
        id=str(existing.id), store_id=str(existing.store_id),
        key=existing.key, value=existing.value,
        updated_by=str(existing.updated_by) if existing.updated_by else None,
        updated_at=existing.updated_at,
    )


@router.delete("/stores/{store_id}/{key}", status_code=204)
async def delete_store_setting(
    store_id: UUID,
    key: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> None:
    """매장 설정 override 제거."""
    existing = await db.scalar(
        select(StoreSetting).where(
            StoreSetting.store_id == store_id,
            StoreSetting.key == key,
        )
    )
    if existing is None:
        return
    await db.delete(existing)
    await db.commit()


# ─── Resolve ───────────────────────────────────────

@router.get("/resolve", response_model=ResolvedSettingResponse)
async def resolve(
    key: Annotated[str, Query()],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:read"))],
    store_id: Annotated[str | None, Query()] = None,
    user_id: Annotated[str | None, Query()] = None,
) -> ResolvedSettingResponse:
    """Setting 키 값 해결 (resolver utility 호출)."""
    try:
        value = await resolve_setting(
            db, key,
            organization_id=current_user.organization_id,
            store_id=UUID(store_id) if store_id else None,
            user_id=UUID(user_id) if user_id else None,
        )
    except SettingNotRegisteredError as e:
        raise NotFoundError(str(e))
    # source 추정 (간단화: 정확한 source 추적은 resolver를 확장해야 함)
    return ResolvedSettingResponse(key=key, value=value, source="resolved")
