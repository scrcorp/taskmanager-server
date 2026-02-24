"""체크리스트 코멘트 Pydantic 스키마.

Checklist comment request/response schemas.
"""

from datetime import datetime
from pydantic import BaseModel


class ChecklistCommentCreate(BaseModel):
    text: str


class ChecklistCommentResponse(BaseModel):
    id: str
    instance_id: str
    user_id: str
    user_name: str | None = None
    text: str
    created_at: datetime
