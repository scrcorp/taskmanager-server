"""Voice Pydantic 스키마.

Voice request/response schemas.
"""

from pydantic import BaseModel


class VoiceCreate(BaseModel):
    title: str
    description: str | None = None
    category: str = "idea"  # idea, facility, equipment, safety, hr, other
    priority: str = "normal"  # low, normal, high, urgent
    store_id: str | None = None


class VoiceUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    category: str | None = None
    priority: str | None = None
    status: str | None = None  # open, in_progress, resolved
    store_id: str | None = None
