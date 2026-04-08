"""Settings Resolver — Settings Registry 기반 설정 값 해결.

resolve_setting()은 등록된 설정 키에 대해 priority에 따라 최종 값을 반환한다.

Priority 옵션:
    - "item": staff → store → org → default (가장 좁은 범위 우선)
    - "store": store → org → default
    - "org": org → default

force_locked가 org level에 켜져 있으면 store/staff override를 무시하고 org 값을 강제.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import OrgSetting, SettingsRegistry, StaffSetting, StoreSetting


class SettingNotRegisteredError(Exception):
    """settings_registry에 정의되지 않은 키를 조회할 때 발생."""


async def resolve_setting(
    db: AsyncSession,
    key: str,
    organization_id: UUID,
    store_id: UUID | None = None,
    user_id: UUID | None = None,
) -> Any:
    """설정 키의 최종 값을 priority에 따라 해결한다.

    Args:
        db: AsyncSession
        key: settings_registry.key
        organization_id: 필수 (org scope)
        store_id: store-level override 조회용 (선택)
        user_id: staff-level override 조회용 (선택)

    Returns:
        해결된 값 (JSON-deserialized: dict, list, str, int, bool 등)

    Raises:
        SettingNotRegisteredError: registry에 키가 없을 때
    """
    # 1. registry meta 로드
    registry = await db.scalar(select(SettingsRegistry).where(SettingsRegistry.key == key))
    if registry is None:
        raise SettingNotRegisteredError(f"Setting key '{key}' is not registered")

    levels = registry.levels or []
    priority = registry.default_priority or "item"
    default_value = registry.default_value

    # 2. org 값 + force_locked 체크
    org_value = None
    org_locked = False
    if "org" in levels:
        org_setting = await db.scalar(
            select(OrgSetting).where(
                OrgSetting.organization_id == organization_id,
                OrgSetting.key == key,
            )
        )
        if org_setting is not None:
            org_value = org_setting.value
            org_locked = org_setting.force_locked

    if org_locked and org_value is not None:
        return org_value

    # 3. priority별 lookup
    if priority == "item":
        # staff → store → org → default
        if user_id is not None and "staff" in levels:
            staff_setting = await db.scalar(
                select(StaffSetting).where(
                    StaffSetting.user_id == user_id,
                    StaffSetting.key == key,
                )
            )
            if staff_setting is not None:
                return staff_setting.value
        if store_id is not None and "store" in levels:
            store_setting = await db.scalar(
                select(StoreSetting).where(
                    StoreSetting.store_id == store_id,
                    StoreSetting.key == key,
                )
            )
            if store_setting is not None:
                return store_setting.value
        if org_value is not None:
            return org_value
        return default_value

    if priority == "store":
        if store_id is not None and "store" in levels:
            store_setting = await db.scalar(
                select(StoreSetting).where(
                    StoreSetting.store_id == store_id,
                    StoreSetting.key == key,
                )
            )
            if store_setting is not None:
                return store_setting.value
        if org_value is not None:
            return org_value
        return default_value

    # priority == "org" (or fallback)
    if org_value is not None:
        return org_value
    return default_value
