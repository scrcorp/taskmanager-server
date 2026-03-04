from datetime import datetime, date
from pydantic import BaseModel


class DailyReportTemplateSectionResponse(BaseModel):
    id: str
    title: str
    description: str | None = None
    sort_order: int
    is_required: bool


class DailyReportTemplateResponse(BaseModel):
    id: str
    name: str
    sections: list[DailyReportTemplateSectionResponse] = []


class SectionContentUpdate(BaseModel):
    section_id: str
    content: str | None = None


class DailyReportCreate(BaseModel):
    store_id: str
    report_date: str  # YYYY-MM-DD
    period: str  # "lunch" or "dinner"
    template_id: str | None = None


class DailyReportUpdate(BaseModel):
    sections: list[SectionContentUpdate]


class DailyReportSectionResponse(BaseModel):
    id: str
    template_section_id: str | None = None
    title: str
    content: str | None = None
    sort_order: int


class DailyReportCommentResponse(BaseModel):
    id: str
    user_id: str
    user_name: str | None = None
    content: str
    created_at: datetime


class DailyReportCommentCreate(BaseModel):
    content: str


class DailyReportResponse(BaseModel):
    id: str
    organization_id: str
    store_id: str
    store_name: str | None = None
    template_id: str | None = None
    author_id: str
    author_name: str | None = None
    report_date: date
    period: str
    status: str
    submitted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    sections: list[DailyReportSectionResponse] = []
    comments: list[DailyReportCommentResponse] = []


class DailyReportListResponse(BaseModel):
    id: str
    store_id: str
    store_name: str | None = None
    author_id: str
    author_name: str | None = None
    report_date: date
    period: str
    status: str
    submitted_at: datetime | None = None
    created_at: datetime
