"""공통 Pydantic 요청/응답 스키마 정의.

Common Pydantic request/response schema definitions.
Includes schemas for checklists, assignments, announcements,
additional tasks, notifications, pagination, and generic messages.
This module consolidates schemas used across multiple API domains.
"""

from datetime import date, datetime
from typing import Any
from pydantic import BaseModel, Field


# === 체크리스트 (Checklist) 스키마 ===

class ChecklistTemplateCreate(BaseModel):
    """체크리스트 템플릿 생성 요청 스키마.

    Checklist template creation request schema.
    Creates a template for a unique brand + shift + position combination.
    brand_id is provided via URL path parameter, not in the request body.

    Attributes:
        shift_id: 대상 시간대 UUID (Target shift identifier)
        position_id: 대상 포지션 UUID (Target position identifier)
        title: 템플릿 제목 (Template title)
    """

    shift_id: str  # 대상 시간대 UUID (Shift identifier)
    position_id: str  # 대상 포지션 UUID (Position identifier)
    title: str  # 템플릿 제목 (Template display title)


class ChecklistTemplateResponse(BaseModel):
    """체크리스트 템플릿 응답 스키마.

    Checklist template response schema with item count.

    Attributes:
        id: 템플릿 UUID (Template unique identifier)
        brand_id: 브랜드 UUID (Brand identifier)
        shift_id: 시간대 UUID (Shift identifier)
        position_id: 포지션 UUID (Position identifier)
        title: 템플릿 제목 (Template title)
        item_count: 항목 수 (Number of items in the template)
    """

    id: str  # 템플릿 UUID 문자열 (Template UUID as string)
    brand_id: str  # 브랜드 UUID 문자열 (Brand UUID as string)
    shift_id: str  # 시간대 UUID 문자열 (Shift UUID as string)
    position_id: str  # 포지션 UUID 문자열 (Position UUID as string)
    shift_name: str = ""  # 시간대 이름 (Shift display name)
    position_name: str = ""  # 포지션 이름 (Position display name)
    title: str  # 템플릿 제목 (Template title)
    item_count: int = 0  # 항목 수 — 서비스에서 계산 (Item count, computed by service)


class ChecklistTemplateUpdate(BaseModel):
    """체크리스트 템플릿 수정 요청 스키마 (부분 업데이트).

    Checklist template update request schema (partial update).

    Attributes:
        title: 변경할 템플릿 제목 (New template title, optional)
        shift_id: 변경할 시간대 UUID (New shift identifier, optional)
        position_id: 변경할 포지션 UUID (New position identifier, optional)
    """

    title: str | None = None  # 변경할 템플릿 제목 (New title, optional)
    shift_id: str | None = None  # 변경할 시간대 UUID (New shift identifier, optional)
    position_id: str | None = None  # 변경할 포지션 UUID (New position identifier, optional)


class ChecklistItemCreate(BaseModel):
    """체크리스트 항목 생성 요청 스키마.

    Checklist item creation request schema.
    Adds a new item to an existing checklist template.

    Attributes:
        title: 항목 제목 (Item title/task description)
        description: 상세 설명 (Detailed description, optional)
        verification_type: 확인 유형 (Verification: "none", "photo", "text")
        sort_order: 정렬 순서 (Display order, default 0)
    """

    title: str  # 항목 제목 (Task title)
    description: str | None = None  # 상세 설명 (Detailed instructions, optional)
    verification_type: str = "none"  # 확인 유형 — "none"|"photo"|"text" (Verification method)
    sort_order: int = 0  # 정렬 순서 (Display order)


class ChecklistItemUpdate(BaseModel):
    """체크리스트 항목 수정 요청 스키마 (부분 업데이트).

    Checklist item update request schema (partial update).

    Attributes:
        title: 항목 제목 (New title, optional)
        description: 상세 설명 (New description, optional)
        verification_type: 확인 유형 (New verification type, optional)
        sort_order: 정렬 순서 (New sort order, optional)
    """

    title: str | None = None  # 변경할 항목 제목 (New title, optional)
    description: str | None = None  # 변경할 설명 (New description, optional)
    verification_type: str | None = None  # 변경할 확인 유형 (New verification type, optional)
    sort_order: int | None = None  # 변경할 정렬 순서 (New sort order, optional)


class ChecklistItemResponse(BaseModel):
    """체크리스트 항목 응답 스키마.

    Checklist item response schema.

    Attributes:
        id: 항목 UUID (Item unique identifier)
        title: 항목 제목 (Item title)
        description: 상세 설명 (Description, nullable)
        verification_type: 확인 유형 (Verification method)
        sort_order: 정렬 순서 (Display order)
    """

    id: str  # 항목 UUID 문자열 (Item UUID as string)
    title: str  # 항목 제목 (Item title)
    description: str | None  # 상세 설명 (Description, may be null)
    verification_type: str  # 확인 유형 — "none"|"photo"|"text" (Verification method)
    sort_order: int  # 정렬 순서 (Display order)


class ChecklistBulkItemCreate(BaseModel):
    """체크리스트 항목 일괄 생성 요청 스키마.

    Bulk checklist item creation request schema.
    Creates multiple items in a single atomic transaction.

    Attributes:
        items: 생성할 항목 목록 (List of items to create)
    """

    items: list[ChecklistItemCreate] = Field(..., min_length=1)  # 일괄 생성할 항목 목록 (Items to bulk-create, at least 1)


class ReorderRequest(BaseModel):
    """항목 재정렬 요청 스키마.

    Item reorder request schema for drag-and-drop reordering.
    The order of item_ids determines the new sort_order values.

    Attributes:
        item_ids: 재정렬된 항목 UUID 목록 (Ordered list of item UUIDs)
    """

    item_ids: list[str]  # 새 순서대로 나열된 항목 UUID 목록 (Item UUIDs in desired order)


# === 근무 배정 (Assignment) 스키마 ===

class AssignmentCreate(BaseModel):
    """근무 배정 생성 요청 스키마.

    Work assignment creation request schema.
    Creates a daily assignment for a user at a specific brand/shift/position.
    The server automatically snapshots the matching checklist template.

    Attributes:
        brand_id: 대상 브랜드 UUID (Target brand)
        shift_id: 대상 시간대 UUID (Target shift)
        position_id: 대상 포지션 UUID (Target position)
        user_id: 배정 대상 사용자 UUID (Worker to assign)
        work_date: 근무 날짜 (Work date)
    """

    brand_id: str  # 대상 브랜드 UUID (Brand identifier)
    shift_id: str  # 대상 시간대 UUID (Shift identifier)
    position_id: str  # 대상 포지션 UUID (Position identifier)
    user_id: str  # 배정 대상 사용자 UUID (Worker identifier)
    work_date: date  # 근무 날짜 — 시간 없이 날짜만 (Date only, no time component)


class AssignmentResponse(BaseModel):
    """근무 배정 응답 스키마 (목록용).

    Work assignment response schema for list views.
    Includes resolved names for brand, shift, position, and user
    to avoid additional API calls on the client.

    Attributes:
        id: 배정 UUID (Assignment unique identifier)
        brand_id: 브랜드 UUID (Brand identifier)
        brand_name: 브랜드 이름 (Resolved brand name)
        shift_id: 시간대 UUID (Shift identifier)
        shift_name: 시간대 이름 (Resolved shift name)
        position_id: 포지션 UUID (Position identifier)
        position_name: 포지션 이름 (Resolved position name)
        user_id: 사용자 UUID (Worker identifier)
        user_name: 사용자 이름 (Resolved worker name)
        work_date: 근무 날짜 (Assignment date)
        status: 진행 상태 (Status: assigned/in_progress/completed)
        total_items: 총 항목 수 (Total checklist items)
        completed_items: 완료 항목 수 (Completed checklist items)
        created_at: 생성 일시 (Creation timestamp)
    """

    id: str  # 배정 UUID 문자열 (Assignment UUID as string)
    brand_id: str  # 브랜드 UUID 문자열 (Brand UUID as string)
    brand_name: str  # 브랜드 이름 — 조인된 값 (Brand name, resolved)
    shift_id: str  # 시간대 UUID 문자열 (Shift UUID as string)
    shift_name: str  # 시간대 이름 — 조인된 값 (Shift name, resolved)
    position_id: str  # 포지션 UUID 문자열 (Position UUID as string)
    position_name: str  # 포지션 이름 — 조인된 값 (Position name, resolved)
    user_id: str  # 사용자 UUID 문자열 (Worker UUID as string)
    user_name: str  # 사용자 이름 — 조인된 값 (Worker name, resolved)
    work_date: date  # 근무 날짜 (Work date)
    status: str  # 진행 상태 — "assigned"|"in_progress"|"completed" (Workflow status)
    total_items: int  # 총 체크리스트 항목 수 (Total items for progress display)
    completed_items: int  # 완료된 항목 수 (Completed items for progress display)
    created_at: datetime  # 생성 일시 UTC (Assignment creation timestamp)


class AssignmentDetailResponse(AssignmentResponse):
    """근무 배정 상세 응답 스키마 — 체크리스트 스냅샷 포함.

    Work assignment detail response with full JSONB checklist snapshot.
    Used when viewing a single assignment with all checklist items.

    Attributes:
        checklist_snapshot: JSONB 체크리스트 스냅샷 (Snapshot of checklist items at assignment time)
    """

    # JSONB 스냅샷 — 각 항목: {item_index, title, description, verification_type, is_completed, completed_at}
    checklist_snapshot: list[dict[str, Any]] | None = None


class ChecklistItemComplete(BaseModel):
    """체크리스트 항목 완료 토글 요청 스키마.

    Checklist item completion toggle request schema.
    Used to mark/unmark individual checklist items within an assignment.

    Attributes:
        is_completed: 완료 여부 (True=완료, False=미완료)
    """

    is_completed: bool  # 완료 여부 — True이면 completed_at 자동 설정 (Completion flag)


# === 공지사항 (Announcement) 스키마 ===

class AnnouncementCreate(BaseModel):
    """공지사항 생성 요청 스키마.

    Announcement creation request schema.
    If brand_id is null, the announcement targets the entire organization.

    Attributes:
        title: 공지 제목 (Announcement title)
        content: 공지 내용 (Announcement body text)
        brand_id: 대상 브랜드 UUID (Target brand, null = org-wide)
    """

    title: str  # 공지 제목 (Announcement title)
    content: str  # 공지 내용 (Announcement body)
    brand_id: str | None = None  # 대상 브랜드 — None이면 조직 전체 (Brand scope, null = org-wide)


class AnnouncementUpdate(BaseModel):
    """공지사항 수정 요청 스키마 (부분 업데이트).

    Announcement update request schema (partial update).

    Attributes:
        title: 공지 제목 (New title, optional)
        content: 공지 내용 (New content, optional)
    """

    title: str | None = None  # 변경할 공지 제목 (New title, optional)
    content: str | None = None  # 변경할 공지 내용 (New content, optional)


class AnnouncementResponse(BaseModel):
    """공지사항 응답 스키마.

    Announcement response schema with resolved names.

    Attributes:
        id: 공지 UUID (Announcement unique identifier)
        title: 공지 제목 (Announcement title)
        content: 공지 내용 (Announcement body)
        brand_id: 대상 브랜드 UUID (Target brand, nullable)
        brand_name: 대상 브랜드 이름 (Resolved brand name, nullable)
        created_by_name: 작성자 이름 (Resolved author name)
        created_at: 생성 일시 (Creation timestamp)
    """

    id: str  # 공지 UUID 문자열 (Announcement UUID as string)
    title: str  # 공지 제목 (Title)
    content: str  # 공지 내용 (Body text)
    brand_id: str | None  # 대상 브랜드 UUID — None이면 조직 전체 (Brand scope, null = org-wide)
    brand_name: str | None  # 대상 브랜드 이름 — 조인된 값 (Brand name, resolved)
    created_by_name: str  # 작성자 이름 — 조인된 값 (Author name, resolved)
    created_at: datetime  # 생성 일시 UTC (Creation timestamp)


# === 추가 업무 (Additional Task) 스키마 ===

class TaskCreate(BaseModel):
    """추가 업무 생성 요청 스키마.

    Additional task creation request schema.
    Creates an ad-hoc task with optional assignees and due date.

    Attributes:
        title: 업무 제목 (Task title)
        description: 업무 설명 (Task description, optional)
        brand_id: 대상 브랜드 UUID (Brand scope, optional)
        priority: 우선순위 (Priority: "normal" or "urgent")
        due_date: 마감일시 (Due date with timezone, optional)
        assignee_ids: 담당자 UUID 목록 (List of assignee user UUIDs)
    """

    title: str  # 업무 제목 (Task title)
    description: str | None = None  # 업무 설명 (Description, optional)
    brand_id: str | None = None  # 대상 브랜드 — None이면 조직 전체 (Brand scope, optional)
    priority: str = "normal"  # 우선순위 — "normal"|"urgent" (Priority level)
    due_date: datetime | None = None  # 마감일시 (Deadline, optional)
    assignee_ids: list[str] = []  # 담당자 UUID 목록 (Worker UUIDs to assign)


class TaskUpdate(BaseModel):
    """추가 업무 수정 요청 스키마 (부분 업데이트).

    Additional task update request schema (partial update).

    Attributes:
        title: 업무 제목 (New title, optional)
        description: 업무 설명 (New description, optional)
        priority: 우선순위 (New priority, optional)
        status: 진행 상태 (New status, optional)
        due_date: 마감일시 (New due date, optional)
    """

    title: str | None = None  # 변경할 업무 제목 (New title, optional)
    description: str | None = None  # 변경할 설명 (New description, optional)
    priority: str | None = None  # 변경할 우선순위 (New priority, optional)
    status: str | None = None  # 변경할 상태 (New status, optional)
    due_date: datetime | None = None  # 변경할 마감일시 (New due date, optional)


class TaskResponse(BaseModel):
    """추가 업무 응답 스키마.

    Additional task response schema with resolved names.

    Attributes:
        id: 업무 UUID (Task unique identifier)
        title: 업무 제목 (Task title)
        description: 업무 설명 (Description, nullable)
        brand_id: 브랜드 UUID (Brand scope, nullable)
        brand_name: 브랜드 이름 (Resolved brand name, nullable)
        priority: 우선순위 (Priority: "normal" or "urgent")
        status: 진행 상태 (Status: pending/in_progress/completed)
        due_date: 마감일시 (Due date, nullable)
        created_by_name: 생성자 이름 (Resolved creator name)
        assignee_names: 담당자 이름 목록 (Resolved assignee names)
        created_at: 생성 일시 (Creation timestamp)
    """

    id: str  # 업무 UUID 문자열 (Task UUID as string)
    title: str  # 업무 제목 (Task title)
    description: str | None  # 업무 설명 (Description, may be null)
    brand_id: str | None  # 브랜드 UUID — None이면 조직 전체 (Brand scope, null = org-wide)
    brand_name: str | None  # 브랜드 이름 — 조인된 값 (Brand name, resolved)
    priority: str  # 우선순위 — "normal"|"urgent" (Priority level)
    status: str  # 진행 상태 — "pending"|"in_progress"|"completed" (Workflow status)
    due_date: datetime | None  # 마감일시 (Deadline, may be null)
    created_by_name: str  # 생성자 이름 — 조인된 값 (Creator name, resolved)
    assignee_names: list[str] = []  # 담당자 이름 목록 — 조인된 값 (Assignee names, resolved)
    created_at: datetime  # 생성 일시 UTC (Creation timestamp)


# === 알림 (Notification) 스키마 ===

class NotificationResponse(BaseModel):
    """알림 응답 스키마.

    Notification response schema.
    Uses polymorphic reference_type + reference_id for deep-linking
    to the source entity in the client app.

    Attributes:
        id: 알림 UUID (Notification unique identifier)
        type: 알림 유형 (Notification type)
        message: 알림 메시지 (Human-readable message)
        reference_type: 참조 엔티티 유형 (Source entity type, nullable)
        reference_id: 참조 엔티티 UUID (Source entity UUID, nullable)
        is_read: 읽음 여부 (Read status flag)
        created_at: 생성 일시 (Creation timestamp)
    """

    id: str  # 알림 UUID 문자열 (Notification UUID as string)
    type: str  # 알림 유형 — "work_assigned"|"additional_task"|"announcement"|"task_completed"
    message: str  # 알림 메시지 (Display message)
    reference_type: str | None  # 참조 엔티티 유형 — 딥링크용 (Entity type for deep-linking)
    reference_id: str | None  # 참조 엔티티 UUID — 딥링크용 (Entity UUID for deep-linking)
    is_read: bool  # 읽음 여부 (Read flag)
    created_at: datetime  # 생성 일시 UTC (Creation timestamp)


# === 공통 (Common) 스키마 ===

class PaginatedResponse(BaseModel):
    """페이지네이션 응답 스키마.

    Paginated response wrapper schema.
    Wraps a list of items with pagination metadata.

    Attributes:
        items: 항목 목록 (List of result items)
        total: 전체 항목 수 (Total count across all pages)
        page: 현재 페이지 번호 (Current page number, 1-based)
        per_page: 페이지당 항목 수 (Items per page)
    """

    items: list[Any]  # 결과 항목 목록 (List of items for the current page)
    total: int  # 전체 항목 수 (Total item count)
    page: int  # 현재 페이지 — 1부터 시작 (Current page, 1-indexed)
    per_page: int  # 페이지당 항목 수 (Items per page)


class MessageResponse(BaseModel):
    """범용 메시지 응답 스키마.

    Generic message response schema for simple confirmations.
    Used for delete operations, status changes, and other actions
    that return a human-readable confirmation message.

    Attributes:
        message: 응답 메시지 (Response message string)
    """

    message: str  # 응답 메시지 (Human-readable confirmation message)
