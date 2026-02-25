"""이슈 리포트 Pydantic 스키마.

Issue report request/response schemas.
"""

from pydantic import BaseModel


class IssueReportCreate(BaseModel):
    title: str
    description: str | None = None
    category: str = "other"  # facility, equipment, safety, hr, other
    priority: str = "normal"  # low, normal, high, urgent
    store_id: str | None = None


class IssueReportUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    category: str | None = None
    priority: str | None = None
    status: str | None = None  # open, in_progress, resolved
    store_id: str | None = None
