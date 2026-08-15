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
DEFAULT_ISSUE_CATEGORIES = [
    "equipment", "safety", "customer", "staff", "inventory", "review", "other",
]
ISSUE_SEVERITIES = ["low", "medium", "high", "critical"]
ISSUE_STATUSES = ["open", "in_progress", "closed"]

# 카테고리를 고르면 description 에 프리필할 "제목 줄" 원문 (클라가 프리필, 서버는 저장만).
# 키가 없는 카테고리는 프리셋 없음.
#
# 항목 사이에 빈 줄을 넣는다 — 줄이 붙어 있으면 작성자가 어디에 뭘 쓰는지 구분이 안 된다.
DEFAULT_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES: dict[str, str] = {
    "review": (
        "Platform:\n\n"
        "Rating:\n\n"
        "What was said:\n\n"
        "How we responded:\n\n"
        "Follow-up needed:\n\n"
        "Plan:"
    ),
}

# 지난 버전의 프리셋 원문. startup 보정이 **이 값들만** 새 원문으로 갈아끼운다.
# (운영자가 직접 고친 문구나 null 로 비운 항목은 건드리지 않는다)
LEGACY_ISSUE_CATEGORY_DESCRIPTION_TEMPLATES: dict[str, list[str]] = {
    "review": [
        "Platform:\nRating:\nWhat was said:\nHow we responded:\nFollow-up needed:\nPlan:",
    ],
}

# 조회 범위 — **확대 전용**. 축소(author_only)는 만들지 않는다(은폐 방지).
#   default   : 현행 규칙 (작성자 + 그 매장 manager 중 작성자보다 상위 직급 + extra_viewers)
#   managers  : + 그 매장 manager 전원 (직급 무관)
#   store_all : + 그 매장 배정 인원 전원
ISSUE_VISIBILITY_SCOPES = ["default", "managers", "store_all"]


class IssueCategoryDef(BaseModel):
    """매장별 이슈 카테고리 정의 (template payload 안에 들어감)."""
    code: str  # 영문 식별자 (e.g. "equipment", "kitchen_fire")
    label: str  # UI 표시명 (e.g. "Equipment", "Kitchen Fire")
    color: str | None = None  # 표시용 hex color
    sort_order: int = 0
    is_active: bool = True
    # 이 카테고리를 고르면 작성 화면 description 에 프리필할 원문. None = 프리셋 없음.
    description_template: str | None = None


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
    """콕 집어 추가한 수신자 — 조회권 + 이메일 알림을 함께 받는다.

    position_ids 는 미구현 (position-user 매핑 도입 전까지 무시).
    """
    user_ids: list[str] = []
    position_ids: list[str] = []


class IssueReportPayload(BaseModel):
    category: str  # store template의 categories 중 하나
    severity: str  # ISSUE_SEVERITIES 중 하나
    description: str | None = None
    attachments: list[IssueAttachment] = []
    links: IssueLinks = Field(default_factory=IssueLinks)
    extra_viewers: IssueExtraViewers = Field(default_factory=IssueExtraViewers)
    # 확대 전용 조회 범위 (ISSUE_VISIBILITY_SCOPES). 키 없음 = "default".
    visibility_scope: str = "default"
    # DEPRECATED (2026-08-14) — 자동 수신자(그 매장 GM 이상)는 해제 불가가 되어
    # 이 키는 **아무 효과가 없다**. 구버전 클라가 계속 보내므로 받기만 하고 무시한다.
    notify_excluded_user_ids: list[str] = []
    # 매장별 커스텀 필드 응답 (field_id → value)
    custom_field_values: dict[str, Any] = Field(default_factory=dict)
    # 향후 promote 시 채워짐. 신규 키는 linked_task_id, 구버전 데이터는 linked_issue_id 도 인식.
    linked_task_id: str | None = None
    linked_issue_id: str | None = None  # legacy, backward compat


# ── Issue notification recipients ──────────────────────────────────


class IssueRecipientItem(BaseModel):
    """이슈 알림 수신자 1명.

    source: "auto" = 그 매장에 배정된 GM 이상 (항상 수신, 해제 불가),
            "added" = payload.extra_viewers.user_ids 로 콕 집어 추가한 사람.
    is_recipient: 지금 실제로 알림을 받는가. 새 규칙에서는 항상 True
                  (auto 는 해제 불가, added 는 지우면 목록에서 사라진다).
                  구버전 클라 호환으로 필드는 남긴다.
    can_remove: 작성 화면에서 이 사람을 뺄 수 있는가. auto=False(잠김), added=True.
    """
    user_id: str
    full_name: str
    role_label: str
    role_priority: int
    source: str
    is_recipient: bool
    can_remove: bool = True


class IssueRecipientsResponse(BaseModel):
    store_id: str
    report_id: str | None = None
    items: list[IssueRecipientItem] = []


class IssueExpectedViewerItem(BaseModel):
    """선택한 조회 범위에서 이 리포트를 볼 수 있게 되는 사람 1명.

    reason: "author" | "gm_or_above" | "store_manager" | "added"
    reason_label: 그대로 UI 에 찍을 영어 문구 (클라가 코드→문구 매핑을 또 만들지 않게).
    is_notified: 조회권만이 아니라 알림까지 받는가.
    """
    user_id: str
    full_name: str
    role_label: str
    role_priority: int
    reason: str
    reason_label: str
    is_notified: bool


class IssueExpectedViewersSummary(BaseModel):
    label: str
    count: int


class IssueExpectedViewersResponse(BaseModel):
    """조회 범위 미리보기.

    mode="list"    : items 에 실제 사람 목록 (default / managers)
    mode="summary" : items 는 빈 배열, summary 만 (store_all — 인원이 많아 목록을 만들지 않는다)
    """
    store_id: str
    report_id: str | None = None
    scope: str
    mode: str
    summary: IssueExpectedViewersSummary
    items: list[IssueExpectedViewerItem] = []


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
