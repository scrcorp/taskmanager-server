"""공지사항 읽음 추적 Pydantic 스키마.

Announcement read tracking request/response schemas.
"""

from datetime import datetime
from pydantic import BaseModel


class AnnouncementReadResponse(BaseModel):
    user_id: str
    user_name: str | None = None
    read_at: datetime
