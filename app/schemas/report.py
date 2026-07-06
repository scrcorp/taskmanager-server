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
    # 촬영시각 메타 — 받으면 JSONB 에 그대로 보존(강제 없음). 신뢰 앵커는 report 의 created_at.
    capture_time: datetime | None = None
    capture_source: str | None = None  # live | gallery | unknown


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
    # 적용 report_type code 배열. null 또는 [] = 해당 type 의 모든 report_type 에 적용. 결정-9.
    applicable_types: list[str] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ReportTemplateUpdate(BaseModel):
    name: str | None = None
    is_default: bool | None = None
    is_active: bool | None = None
    applicable_types: list[str] | None = None
    payload: dict[str, Any] | None = None


class ReportTemplateResponse(BaseModel):
    id: str
    type: str
    organization_id: str | None = None
    store_id: str | None = None
    name: str
    is_default: bool = False
    is_active: bool = True
    applicable_types: list[str] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


# ── Report Types (daily 'period' 종류 — org-default + store override) ──

# org 에 report_types row 가 하나도 없을 때 사용하는 내장 기본값(결정-7).
# morning 은 존재하나 기본 비활성.
DEFAULT_REPORT_TYPE_DEFS: list[dict[str, Any]] = [
    {"code": "morning", "label": "Morning", "sort_order": 0, "is_active": False},
    {"code": "lunch", "label": "Lunch", "sort_order": 1, "is_active": True},
    {"code": "dinner", "label": "Dinner", "sort_order": 2, "is_active": True},
]


class ReportTypeCreate(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=100)
    store_id: str | None = None  # null = org-default; set = store override/add
    sort_order: int = 0
    is_active: bool = True
    default_deadline_local_time: str | None = None  # "HH:MM"
    deadline_day_offset: int = 0


class ReportTypeUpdate(BaseModel):
    label: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    default_deadline_local_time: str | None = None
    deadline_day_offset: int | None = None


class ReportTypeReorderItem(BaseModel):
    id: str
    sort_order: int


class ReportTypeReorder(BaseModel):
    items: list[ReportTypeReorderItem]


class ReportTypeResponse(BaseModel):
    id: str
    organization_id: str
    store_id: str | None = None
    code: str
    label: str
    sort_order: int = 0
    is_active: bool = True
    default_deadline_local_time: str | None = None
    deadline_day_offset: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EffectiveReportType(BaseModel):
    """매장에 실제로 적용되는 resolved report type (org+store 병합 결과)."""
    code: str
    label: str
    sort_order: int = 0
    is_active: bool = True
    default_deadline_local_time: str | None = None
    deadline_day_offset: int = 0
    scope: str = "org"  # "org" | "store"
    # 이 type 을 편집할 때 PUT 대상이 되는 row id (store override 면 store row, 아니면 org row).
    # 내장 기본값(DB row 없음)이면 None.
    id: str | None = None
    # store override 가 가리키는 org-default row id (있으면).
    org_type_id: str | None = None


# ── Review / Acknowledge (P3) ──────────────────────────────────────

class ReportReviewRequest(BaseModel):
    feedback: str | None = None  # 선택 코멘트 (작성자에게 전달)


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
    deadline_at: datetime | None = None
    is_overdue: bool = False  # 마감 지남 + 아직 미제출 (display only)
    is_late: bool = False  # 마감 이후 제출됨 (display only)
    reviewed_by_id: str | None = None
    reviewed_by_name: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    comment_count: int = 0
    comments: list[dict] = []
    acknowledgement_count: int = 0
    acknowledgements: list[dict] = []
