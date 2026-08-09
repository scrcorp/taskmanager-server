"""Store 설정 write 단일 관문.

console (`PUT /console/settings/stores/{store_id}`) 과 키오스크 manage 모드
(`PUT /attendance/manage/store-settings`) 가 같은 store 설정을 쓴다. 두 경로가
각자 upsert 를 구현하면 registry/force_locked 검증이 갈라져 값이 어긋날 수 있으므로
쓰기는 이 함수 하나만 통과시킨다.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import OrgSetting, SettingsRegistry, StoreSetting
from app.utils.exceptions import BadRequestError, ForbiddenError


async def upsert_store_setting(
    db: AsyncSession,
    *,
    store_id: UUID,
    organization_id: UUID,
    key: str,
    value: Any,
    updated_by: UUID | None,
) -> StoreSetting:
    """store 설정 upsert. registry 등록 / store level 허용 / org force_locked 검증.

    commit 은 호출 측 책임 (라우터마다 트랜잭션 경계가 다르다).
    """
    registry = await db.scalar(select(SettingsRegistry).where(SettingsRegistry.key == key))
    if registry is None:
        raise BadRequestError(f"Setting key '{key}' is not registered")
    if "store" not in (registry.levels or []):
        raise BadRequestError(f"Setting '{key}' does not allow store-level override")

    org_setting = await db.scalar(
        select(OrgSetting).where(
            OrgSetting.organization_id == organization_id,
            OrgSetting.key == key,
        )
    )
    if org_setting and org_setting.force_locked:
        raise ForbiddenError(f"Setting '{key}' is locked at organization level")

    existing = await db.scalar(
        select(StoreSetting).where(
            StoreSetting.store_id == store_id,
            StoreSetting.key == key,
        )
    )
    if existing is None:
        existing = StoreSetting(
            store_id=store_id, key=key, value=value, updated_by=updated_by
        )
        db.add(existing)
    else:
        existing.value = value
        existing.updated_by = updated_by
        existing.updated_at = datetime.now(timezone.utc)
    return existing
