"""Voice Pydantic 스키마.

Voice request/response schemas.
"""

from pydantic import BaseModel


class VoiceCreate(BaseModel):
    title: str | None = None  # 홈에서 보낼 때 자동 생성 (타임스탬프)
    content: str
    category: str = "idea"  # idea, facility, equipment, safety, hr, other
    priority: str = "normal"  # low, normal, high, urgent
    store_id: str | None = None


class VoiceUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    category: str | None = None
    priority: str | None = None
    status: str | None = None  # open, in_progress, resolved
    store_id: str | None = None
