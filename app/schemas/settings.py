"""Settings Registry / Org / Store / Staff settings 스키마."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class SettingsRegistryResponse(BaseModel):
    key: str
    label: str
    description: str | None = None
    value_type: str
    levels: list[str]
    default_priority: str
    default_value: Any
    validation_schema: dict | None = None
    category: str | None = None
    created_at: datetime
    updated_at: datetime


class SettingsRegistryUpsert(BaseModel):
    """Registry 메타 등록/수정 — admin only."""
    key: str
    label: str
    description: str | None = None
    value_type: str  # number | boolean | string | json
    levels: list[str]  # ["org", "store", "staff"]
    default_priority: str = "item"
    default_value: Any
    validation_schema: dict | None = None
    category: str | None = None


class OrgSettingResponse(BaseModel):
    id: str
    organization_id: str
    key: str
    value: Any
    force_locked: bool
    updated_by: str | None = None
    updated_at: datetime


class OrgSettingUpsert(BaseModel):
    key: str
    value: Any
    force_locked: bool = False


class StoreSettingResponse(BaseModel):
    id: str
    store_id: str
    key: str
    value: Any
    updated_by: str | None = None
    updated_at: datetime


class StoreSettingUpsert(BaseModel):
    key: str
    value: Any


class StaffSettingResponse(BaseModel):
    id: str
    user_id: str
    key: str
    value: Any
    updated_by: str | None = None
    updated_at: datetime


class StaffSettingUpsert(BaseModel):
    key: str
    value: Any


class ResolvedSettingResponse(BaseModel):
    key: str
    value: Any
    source: str  # "staff" | "store" | "org" | "default" | "org_locked"
