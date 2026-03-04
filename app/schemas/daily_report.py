from datetime import datetime, date
from pydantic import BaseModel


# --- Template CRUD Schemas ---

class DailyReportTemplateSectionInput(BaseModel):
    title: str
    description: str | None = None
    sort_order: int = 0
    is_required: bool = False


class DailyReportTemplateCreate(BaseModel):
    name: str
    store_id: str | None = None
    is_default: bool = False
    sections: list[DailyReportTemplateSectionInput] = []


class DailyReportTemplateUpdate(BaseModel):
    name: str | None = None
    is_default: bool | None = None
    is_active: bool | None = None
    sections: list[DailyReportTemplateSectionInput] | None = None


class DailyReportTemplateSectionResponse(BaseModel):
    id: str
    title: str
    description: str | None = None
    sort_order: int
    is_required: bool


class DailyReportTemplateResponse(BaseModel):
    id: str
    organization_id: str | None = None
    store_id: str | None = None
    name: str
    is_default: bool = False
    is_active: bool = True
    created_at: datetime | None = None
    sections: list[DailyReportTemplateSectionResponse] = []


# --- Report Schemas ---

class SectionContentUpdate(BaseModel):
    sort_order: int
    content: str | None = None


class DailyReportCreate(BaseModel):
    store_id: str
    report_date: str  # YYYY-MM-DD
    period: str  # "lunch" or "dinner"
    template_id: str | None = None


class DailyReportUpdate(BaseModel):
    sections: list[SectionContentUpdate]


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
    comment_count: int = 0
    sections: list[dict] = []
    comments: list[dict] = []
