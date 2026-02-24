"""시프트 프리셋 Pydantic 스키마.

Shift Preset request/response schemas.
"""

from datetime import datetime
from pydantic import BaseModel


class ShiftPresetCreate(BaseModel):
    shift_id: str
    name: str
    start_time: str  # "HH:MM"
    end_time: str  # "HH:MM"
    sort_order: int = 0


class ShiftPresetUpdate(BaseModel):
    name: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class ShiftPresetResponse(BaseModel):
    id: str
    store_id: str
    shift_id: str
    name: str
    start_time: str
    end_time: str
    is_active: bool
    sort_order: int
    created_at: datetime
