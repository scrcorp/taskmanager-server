"""노동법 설정 Pydantic 스키마.

Labor Law Setting request/response schemas.
"""

from datetime import datetime
from pydantic import BaseModel


class LaborLawSettingUpdate(BaseModel):
    federal_max_weekly: int = 40
    state_max_weekly: int | None = None
    store_max_weekly: int | None = None
    overtime_threshold_daily: int | None = None


class LaborLawSettingResponse(BaseModel):
    id: str
    store_id: str
    federal_max_weekly: int
    state_max_weekly: int | None
    store_max_weekly: int | None
    overtime_threshold_daily: int | None
    created_at: datetime
