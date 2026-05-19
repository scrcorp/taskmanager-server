"""Multi-type Report 스키마.

신규 통합 리포트(reports 테이블)용. type 디스크리미네이터로 daily/issue/...
구분하고 payload는 type별로 자유 구조. Phase C에선 daily 타입만 정의하고,
Phase D에서 issue payload 추가 예정.
"""
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Template payload (type별 내부 구조) ─────────────────────────────

class DailyReportTemplateSectionDict(BaseModel):
    """daily 템플릿의 섹션 정의 (payload.sections 안에 들어감)."""
    id: str | None = None
    title: str
    description: str | None = None
    is_required: bool = False
    sort_order: int = 0


class DailyReportTemplatePayload(BaseModel):
    sections: list[DailyReportTemplateSectionDict] = []


# ── Report payload (type별 본문 구조) ──────────────────────────────

class DailyReportSectionDict(BaseModel):
    """daily 리포트 본문의 섹션 (payload.sections 안에 들어감)."""
    id: str | None = None
    title: str
    content: str | None = None
    sort_order: int = 0
    template_section_id: str | None = None


class DailyReportPayload(BaseModel):
    period: str  # "lunch" | "dinner"
    sections: list[DailyReportSectionDict] = []


# ── Issue Report payload ───────────────────────────────────────────

# 시스템 기본 카테고리. store별 customize는 report_templates(type='issue').payload.categories로.
DEFAULT_ISSUE_CATEGORIES = ["equipment", "safety", "customer", "staff", "inventory", "other"]
ISSUE_SEVERITIES = ["low", "medium", "high", "critical"]
ISSUE_STATUSES = ["open", "in_progress", "closed"]


class IssueCategoryDef(BaseModel):
    """매장별 이슈 카테고리 정의 (template payload 안에 들어감)."""
    code: str  # 영문 식별자 (e.g. "equipment", "kitchen_fire")
    label: str  # UI 표시명 (e.g. "Equipment", "Kitchen Fire")
    color: str | None = None  # 표시용 hex color
    sort_order: int = 0
    is_active: bool = True


class IssueCustomFieldDef(BaseModel):
    """매장별 이슈 폼 커스텀 필드 (hiring form builder 차용).

    template payload.custom_fields 배열에 들어감.
    """
    type: str  # "short_text" | "long_text" | "number" | "single_choice" | "multi_choice" | "checkbox"
    id: str  # field 고유 ID (response의 키로 사용)
    label: str
    required: bool = False
    placeholder: str | None = None
    options: list[str] | None = None  # single_choice / multi_choice 용
    max_length: int | None = None
    min: float | None = None
    max: float | None = None
    sort_order: int = 0


class IssueTemplatePayload(BaseModel):
    """매장별 이슈 폼 config. report_templates(type='issue').payload."""
    categories: list[IssueCategoryDef] = []
    custom_fields: list[IssueCustomFieldDef] = []


class IssueAttachment(BaseModel):
    """첨부 파일 (사진/동영상). storage_service.resolve_url로 URL 변환."""
    key: str  # 상대경로 (e.g. issues/2026/05/{uuid}.jpg)
    mime_type: str | None = None
    kind: str | None = None  # "image" | "video"
    name: str | None = None
    size: int | None = None


class IssueLinks(BaseModel):
    """관련 리소스 다중 연결 (FK 검증은 service에서 작성 시점에).

    position_ids / work_role_ids 는 backward-compat. 현재 UI는 schedule + people +
    role 만 노출. role 은 system role name (staff/sv/gm/owner/all).
    """
    schedule_ids: list[str] = []
    checklist_instance_ids: list[str] = []
    position_ids: list[str] = []
    work_role_ids: list[str] = []
    related_user_ids: list[str] = []
    related_roles: list[str] = []


class IssueExtraViewers(BaseModel):
    """기본 조회권자(작성자 + 매장 SV+) 외에 추가로 보게 할 사람/포지션."""
    user_ids: list[str] = []
    position_ids: list[str] = []


class IssueReportPayload(BaseModel):
    category: str  # store template의 categories 중 하나
    severity: str  # ISSUE_SEVERITIES 중 하나
    description: str | None = None
    attachments: list[IssueAttachment] = []
    links: IssueLinks = Field(default_factory=IssueLinks)
    extra_viewers: IssueExtraViewers = Field(default_factory=IssueExtraViewers)
    # 매장별 커스텀 필드 응답 (field_id → value)
    custom_field_values: dict[str, Any] = Field(default_factory=dict)
    # 향후 promote 시 채워짐. 신규 키는 linked_task_id, 구버전 데이터는 linked_issue_id 도 인식.
    linked_task_id: str | None = None
    linked_issue_id: str | None = None  # legacy, backward compat


# ── Template CRUD ──────────────────────────────────────────────────

class ReportTemplateCreate(BaseModel):
    type: str
    name: str
    store_id: str | None = None
    is_default: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)


class ReportTemplateUpdate(BaseModel):
    name: str | None = None
    is_default: bool | None = None
    is_active: bool | None = None
    payload: dict[str, Any] | None = None


class ReportTemplateResponse(BaseModel):
    id: str
    type: str
    organization_id: str | None = None
    store_id: str | None = None
    name: str
    is_default: bool = False
    is_active: bool = True
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


# ── Report CRUD ────────────────────────────────────────────────────

class ReportCreate(BaseModel):
    type: str
    store_id: str
    report_date: str | None = None  # YYYY-MM-DD, daily는 필수
    template_id: str | None = None
    title: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class SectionContentUpdate(BaseModel):
    sort_order: int
    content: str | None = None


class ReportUpdate(BaseModel):
    """payload 일부 또는 전체 업데이트.

    daily의 경우 sections만 수정하는 게 일반적 → sections 필드만 받으면
    service에서 기존 payload를 보존한 채 sections만 교체.
    title, payload(전체)도 선택적으로 받음.
    """
    sections: list[SectionContentUpdate] | None = None
    title: str | None = None
    payload: dict[str, Any] | None = None


class ReportCommentCreate(BaseModel):
    content: str


class ReportResponse(BaseModel):
    id: str
    type: str
    organization_id: str
    store_id: str | None = None
    store_name: str | None = None
    template_id: str | None = None
    author_id: str | None = None
    author_name: str | None = None
    title: str | None = None
    status: str
    report_date: date | None = None
    submitted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    comment_count: int = 0
    comments: list[dict] = []
