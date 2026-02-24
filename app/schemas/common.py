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
    Creates a template for a unique store + shift + position combination.
    store_id is provided via URL path parameter, not in the request body.

    Title is auto-generated as '{store} - {shift} - {position}'.
    If title is provided, format becomes '{store} - {shift} - {position} ({title})'.

    Attributes:
        shift_id: 대상 시간대 UUID (Target shift identifier)
        position_id: 대상 포지션 UUID (Target position identifier)
        title: 추가 제목 (Optional additional title, appended in parentheses)
    """

    shift_id: str  # 대상 시간대 UUID (Shift identifier)
    position_id: str  # 대상 포지션 UUID (Position identifier)
    title: str = ""  # 추가 제목 — 비어있으면 자동 생성 (Optional additional title)


class ChecklistTemplateResponse(BaseModel):
    """체크리스트 템플릿 응답 스키마.

    Checklist template response schema with item count.

    Attributes:
        id: 템플릿 UUID (Template unique identifier)
        store_id: 매장 UUID (Store identifier)
        shift_id: 시간대 UUID (Shift identifier)
        position_id: 포지션 UUID (Position identifier)
        title: 템플릿 제목 (Template title)
        item_count: 항목 수 (Number of items in the template)
    """

    id: str  # 템플릿 UUID 문자열 (Template UUID as string)
    store_id: str  # 매장 UUID 문자열 (Store UUID as string)
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
        verification_type: 확인 유형 (Verification: "none", "photo", "text", or combo "photo,text")
        sort_order: 정렬 순서 (Display order, default 0)
    """

    title: str  # 항목 제목 (Task title)
    description: str | None = None  # 상세 설명 (Detailed instructions, optional)
    verification_type: str = "none"  # 확인 유형 — "none"|"photo"|"text"|"photo,text"
    recurrence_type: str = "daily"  # 반복 주기 — "daily"|"weekly"
    recurrence_days: list[int] | None = None  # weekly일 때 요일 목록 [0=Mon..6=Sun]
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
    recurrence_type: str | None = None  # 변경할 반복 주기 (New recurrence type, optional)
    recurrence_days: list[int] | None = None  # 변경할 반복 요일 (New recurrence days, optional)
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
    verification_type: str  # 확인 유형 — "none"|"photo"|"text"|"photo,text"
    recurrence_type: str = "daily"  # 반복 주기 — "daily"|"weekly"
    recurrence_days: list[int] | None = None  # weekly일 때 요일 목록 [0=Mon..6=Sun]
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


class ExcelImportResponse(BaseModel):
    """Excel import 결과 응답 스키마.

    Excel import result response schema.
    Reports the number of created/skipped entities and any row-level errors.
    """

    created_templates: int = 0
    created_items: int = 0
    created_stores: int = 0
    created_shifts: int = 0
    created_positions: int = 0
    skipped_templates: int = 0
    updated_templates: int = 0
    errors: list[str] = []


# === 근무 배정 (Assignment) 스키마 ===

class AssignmentCreate(BaseModel):
    """근무 배정 생성 요청 스키마.

    Work assignment creation request schema.
    Creates a daily assignment for a user at a specific store/shift/position.
    The server automatically snapshots the matching checklist template.

    Attributes:
        store_id: 대상 매장 UUID (Target store)
        shift_id: 대상 시간대 UUID (Target shift)
        position_id: 대상 포지션 UUID (Target position)
        user_id: 배정 대상 사용자 UUID (Worker to assign)
        work_date: 근무 날짜 (Work date)
    """

    store_id: str  # 대상 매장 UUID (Store identifier)
    shift_id: str  # 대상 시간대 UUID (Shift identifier)
    position_id: str  # 대상 포지션 UUID (Position identifier)
    user_id: str  # 배정 대상 사용자 UUID (Worker identifier)
    work_date: date  # 근무 날짜 — 시간 없이 날짜만 (Date only, no time component)


class AssignmentResponse(BaseModel):
    """근무 배정 응답 스키마 (목록용).

    Work assignment response schema for list views.
    Includes resolved names for store, shift, position, and user
    to avoid additional API calls on the client.

    Attributes:
        id: 배정 UUID (Assignment unique identifier)
        store_id: 매장 UUID (Store identifier)
        store_name: 매장 이름 (Resolved store name)
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
    store_id: str  # 매장 UUID 문자열 (Store UUID as string)
    store_name: str  # 매장 이름 — 조인된 값 (Store name, resolved)
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

    # JSONB 스냅샷 — 각 항목: {item_index, title, description, verification_type, is_completed, completed_at, completed_tz}
    checklist_snapshot: list[dict[str, Any]] | None = None


class ChecklistItemComplete(BaseModel):
    """체크리스트 항목 완료 토글 요청 스키마.

    Checklist item completion toggle request schema.
    Used to mark/unmark individual checklist items within an assignment.

    Attributes:
        is_completed: 완료 여부 (True=완료, False=미완료)
        timezone: 클라이언트 IANA 타임존 (Client IANA timezone, e.g. "America/Los_Angeles")
    """

    is_completed: bool  # 완료 여부 — True이면 completed_at 자동 설정 (Completion flag)
    timezone: str = "America/Los_Angeles"  # 클라이언트 타임존 — 완료 시각 표시용 (Client timezone for display)
    photo_url: str | None = None  # 사진 URL — verification_type이 photo일 때 필수 (Photo URL, optional)
    note: str | None = None  # 메모 — verification_type이 text일 때 필수 (Note, optional)


# === 공지사항 (Announcement) 스키마 ===

class AnnouncementCreate(BaseModel):
    """공지사항 생성 요청 스키마.

    Announcement creation request schema.
    If store_id is null, the announcement targets the entire organization.

    Attributes:
        title: 공지 제목 (Announcement title)
        content: 공지 내용 (Announcement body text)
        store_id: 대상 매장 UUID (Target store, null = org-wide)
    """

    title: str  # 공지 제목 (Announcement title)
    content: str  # 공지 내용 (Announcement body)
    store_id: str | None = None  # 대상 매장 — None이면 조직 전체 (Store scope, null = org-wide)


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
        store_id: 대상 매장 UUID (Target store, nullable)
        store_name: 대상 매장 이름 (Resolved store name, nullable)
        created_by_name: 작성자 이름 (Resolved author name)
        created_at: 생성 일시 (Creation timestamp)
    """

    id: str  # 공지 UUID 문자열 (Announcement UUID as string)
    title: str  # 공지 제목 (Title)
    content: str  # 공지 내용 (Body text)
    store_id: str | None  # 대상 매장 UUID — None이면 조직 전체 (Store scope, null = org-wide)
    store_name: str | None  # 대상 매장 이름 — 조인된 값 (Store name, resolved)
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
        store_id: 대상 매장 UUID (Store scope, optional)
        priority: 우선순위 (Priority: "normal" or "urgent")
        due_date: 마감일시 (Due date with timezone, optional)
        assignee_ids: 담당자 UUID 목록 (List of assignee user UUIDs)
    """

    title: str  # 업무 제목 (Task title)
    description: str | None = None  # 업무 설명 (Description, optional)
    store_id: str | None = None  # 대상 매장 — None이면 조직 전체 (Store scope, optional)
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
        store_id: 매장 UUID (Store scope, nullable)
        store_name: 매장 이름 (Resolved store name, nullable)
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
    store_id: str | None  # 매장 UUID — None이면 조직 전체 (Store scope, null = org-wide)
    store_name: str | None  # 매장 이름 — 조인된 값 (Store name, resolved)
    priority: str  # 우선순위 — "normal"|"urgent" (Priority level)
    status: str  # 진행 상태 — "pending"|"in_progress"|"completed" (Workflow status)
    due_date: datetime | None  # 마감일시 (Deadline, may be null)
    created_by_name: str  # 생성자 이름 — 조인된 값 (Creator name, resolved)
    assignee_names: list[str] = []  # 담당자 이름 목록 — 조인된 값 (Assignee names, resolved)
    created_at: datetime  # 생성 일시 UTC (Creation timestamp)


# === 업무 증빙 (Task Evidence) 스키마 ===

class TaskEvidenceCreate(BaseModel):
    """업무 증빙 생성 요청 스키마.

    Task evidence creation request schema.
    Used when a staff member submits photo/document evidence for an additional task.

    Attributes:
        file_url: 파일 URL (Uploaded file URL from storage)
        file_type: 파일 유형 (File type: "photo" or "document")
        note: 메모 (Optional note for the evidence)
    """

    file_url: str  # 파일 URL — Supabase Storage 또는 S3 (File URL from storage)
    file_type: str = "photo"  # 파일 유형 — "photo"|"document" (File type)
    note: str | None = None  # 메모 (Optional note, nullable)


class TaskEvidenceResponse(BaseModel):
    """업무 증빙 응답 스키마.

    Task evidence response schema with resolved user name.

    Attributes:
        id: 증빙 UUID (Evidence unique identifier)
        task_id: 업무 UUID (Parent task identifier)
        user_id: 제출자 UUID (Submitter user identifier)
        user_name: 제출자 이름 (Resolved submitter name, nullable)
        file_url: 파일 URL (File URL in storage)
        file_type: 파일 유형 (File type: "photo" or "document")
        note: 메모 (Note, nullable)
        created_at: 생성 일시 (Creation timestamp)
    """

    id: str  # 증빙 UUID 문자열 (Evidence UUID as string)
    task_id: str  # 업무 UUID 문자열 (Task UUID as string)
    user_id: str  # 제출자 UUID 문자열 (Submitter UUID as string)
    user_name: str | None = None  # 제출자 이름 — 조인된 값 (Submitter name, resolved)
    file_url: str  # 파일 URL (File URL)
    file_type: str  # 파일 유형 — "photo"|"document" (File type)
    note: str | None  # 메모 (Note, may be null)
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


# === 체크리스트 인스턴스 (Checklist Instance) 스키마 ===

class ChecklistCompletionCreate(BaseModel):
    """체크리스트 항목 완료 생성 요청 스키마.

    Checklist item completion creation request schema.
    Used when a staff member completes an individual checklist item.

    Attributes:
        photo_url: 사진 URL (Photo evidence URL, optional)
        note: 메모 (Text note, optional)
        location: GPS 위치 (lat/lng location data, optional)
    """

    photo_url: str | None = None  # 사진 URL — Supabase Storage (Photo URL, optional)
    note: str | None = None  # 메모 (Text note, optional)
    location: dict | None = None  # GPS 위치 — {lat, lng} (Location data, optional)
    timezone: str = "America/Los_Angeles"  # IANA 타임존 — 완료 시점 로컬 타임존 (Client IANA timezone)


class ChecklistCompletionResponse(BaseModel):
    """체크리스트 항목 완료 응답 스키마.

    Checklist item completion response schema.

    Attributes:
        id: 완료 기록 UUID (Completion record unique identifier)
        item_index: 항목 인덱스 (Snapshot item index)
        user_id: 완료한 사용자 UUID (User who completed the item)
        completed_at: 완료 일시 (Completion timestamp)
        photo_url: 사진 URL (Photo URL, nullable)
        note: 메모 (Note, nullable)
        location: GPS 위치 (Location data, nullable)
    """

    id: str  # 완료 기록 UUID 문자열 (Completion UUID as string)
    item_index: int  # 항목 인덱스 (Snapshot item index)
    user_id: str  # 완료한 사용자 UUID 문자열 (User UUID as string)
    completed_at: datetime  # 완료 일시 UTC (Completion timestamp)
    completed_timezone: str | None  # IANA 타임존 — 로컬 시간 변환용 (IANA timezone for local time)
    photo_url: str | None  # 사진 URL (Photo URL, may be null)
    note: str | None  # 메모 (Note, may be null)
    location: dict | None  # GPS 위치 (Location, may be null)


class ChecklistInstanceResponse(BaseModel):
    """체크리스트 인스턴스 응답 스키마 (목록용).

    Checklist instance response schema for list views.
    Includes progress info and basic metadata.

    Attributes:
        id: 인스턴스 UUID (Instance unique identifier)
        template_id: 원본 템플릿 UUID (Source template, nullable)
        work_assignment_id: 근무 배정 UUID (Work assignment identifier)
        store_id: 매장 UUID (Store identifier)
        user_id: 사용자 UUID (Worker identifier)
        user_name: 사용자 이름 (Resolved worker name)
        store_name: 매장 이름 (Resolved store name)
        work_date: 근무 날짜 (Work date)
        total_items: 총 항목 수 (Total items)
        completed_items: 완료 항목 수 (Completed items)
        status: 진행 상태 (Status)
        created_at: 생성 일시 (Creation timestamp)
    """

    id: str  # 인스턴스 UUID 문자열 (Instance UUID as string)
    template_id: str | None  # 원본 템플릿 UUID — None이면 삭제됨 (Template UUID, null if deleted)
    work_assignment_id: str  # 근무 배정 UUID 문자열 (Assignment UUID as string)
    store_id: str  # 매장 UUID 문자열 (Store UUID as string)
    store_name: str  # 매장 이름 — 조인된 값 (Store name, resolved)
    user_id: str  # 사용자 UUID 문자열 (User UUID as string)
    user_name: str  # 사용자 이름 — 조인된 값 (User name, resolved)
    work_date: date  # 근무 날짜 (Work date)
    total_items: int  # 총 체크리스트 항목 수 (Total items)
    completed_items: int  # 완료된 항목 수 (Completed items)
    status: str  # 진행 상태 — "pending"|"in_progress"|"completed" (Workflow status)
    created_at: datetime  # 생성 일시 UTC (Instance creation timestamp)


class ChecklistInstanceDetailResponse(ChecklistInstanceResponse):
    """체크리스트 인스턴스 상세 응답 스키마 — 스냅샷 + 완료 기록 병합.

    Checklist instance detail response with snapshot items merged with completions.
    Each snapshot item includes its completion data if completed.

    Attributes:
        snapshot: 병합된 스냅샷 (Snapshot items with completion data merged)
    """

    snapshot: list[dict] | None = None  # 병합된 스냅샷 — 각 항목에 completion 정보 포함


# === 스케줄 (Schedule) 스키마 ===

class ScheduleCreate(BaseModel):
    """스케줄 생성 요청 스키마.

    Schedule creation request schema.
    Creates a draft schedule for a user at a specific store on a given date.
    Shift and position are optional; start/end times can be provided directly.

    Attributes:
        store_id: 대상 매장 UUID (Target store)
        user_id: 배정 대상 사용자 UUID (Worker to schedule)
        shift_id: 시간대 UUID, 선택 (Optional shift)
        position_id: 포지션 UUID, 선택 (Optional position)
        work_date: 근무 날짜 (Work date)
        start_time: 시작 시각 문자열 "HH:MM", 선택 (Optional start time)
        end_time: 종료 시각 문자열 "HH:MM", 선택 (Optional end time)
        note: 메모, 선택 (Optional note)
    """

    store_id: str  # 대상 매장 UUID (Store identifier)
    user_id: str  # 배정 대상 사용자 UUID (Worker identifier)
    shift_id: str | None = None  # 시간대 UUID, 선택 (Optional shift identifier)
    position_id: str | None = None  # 포지션 UUID, 선택 (Optional position identifier)
    work_date: date  # 근무 날짜 — 시간 없이 날짜만 (Date only)
    start_time: str | None = None  # 시작 시각 — "09:00" 형식 (Start time, "HH:MM" format)
    end_time: str | None = None  # 종료 시각 — "17:00" 형식 (End time, "HH:MM" format)
    note: str | None = None  # 메모 (Optional note)


class ScheduleUpdate(BaseModel):
    """스케줄 수정 요청 스키마 (부분 업데이트).

    Schedule update request schema (partial update).
    Only provided fields will be updated.

    Attributes:
        shift_id: 시간대 UUID, 선택 (New shift, optional)
        position_id: 포지션 UUID, 선택 (New position, optional)
        start_time: 시작 시각 문자열, 선택 (New start time, optional)
        end_time: 종료 시각 문자열, 선택 (New end time, optional)
        note: 메모, 선택 (New note, optional)
    """

    shift_id: str | None = None  # 변경할 시간대 UUID (New shift identifier, optional)
    position_id: str | None = None  # 변경할 포지션 UUID (New position identifier, optional)
    start_time: str | None = None  # 변경할 시작 시각 (New start time, optional)
    end_time: str | None = None  # 변경할 종료 시각 (New end time, optional)
    note: str | None = None  # 변경할 메모 (New note, optional)


class ScheduleSubstituteRequest(BaseModel):
    """대타 요청 스키마.

    Schedule substitution request schema.

    Attributes:
        new_user_id: 대타 사용자 UUID (Substitute user)
    """

    new_user_id: str  # 대타 사용자 UUID (Substitute user identifier)


class OvertimeValidateRequest(BaseModel):
    """초과근무 사전 검증 요청 스키마.

    Overtime pre-validation request schema.

    Attributes:
        user_id: 사용자 UUID
        work_date: 근무 날짜
        hours: 추가 근무 시간
    """

    user_id: str
    work_date: date
    hours: float


# === 근태 관리 (Attendance) 스키마 ===

class QRCodeResponse(BaseModel):
    """매장 QR 코드 응답 스키마.

    Store QR code response schema.

    Attributes:
        id: QR 코드 UUID (QR code unique identifier)
        store_id: 매장 UUID (Store identifier)
        store_name: 매장 이름 (Resolved store name)
        code: QR 코드 문자열 (Random unique code for QR)
        is_active: 활성 상태 (Whether QR code is active)
        created_at: 생성 일시 (Creation timestamp)
    """

    id: str  # QR 코드 UUID 문자열 (QR code UUID as string)
    store_id: str  # 매장 UUID 문자열 (Store UUID as string)
    store_name: str | None = None  # 매장 이름 — 조인된 값 (Store name, resolved)
    code: str  # QR 코드 문자열 (Random code for QR generation)
    is_active: bool  # 활성 상태 (Active flag)
    created_at: datetime  # 생성 일시 UTC (Creation timestamp)


class AttendanceScanRequest(BaseModel):
    """근태 QR 스캔 요청 스키마.

    Attendance QR scan request schema.
    Used when a user scans a QR code to clock in/out or manage breaks.

    Attributes:
        qr_code: 스캔한 QR 코드 문자열 (Scanned QR code string)
        action: 동작 유형 (Action: clock_in, break_start, break_end, clock_out)
        timezone: 클라이언트 IANA 타임존 (Client IANA timezone)
        location: GPS 위치, 선택 (Optional GPS location {lat, lng})
    """

    qr_code: str  # 스캔한 QR 코드 (Scanned QR code string)
    action: str  # 동작 — "clock_in"|"break_start"|"break_end"|"clock_out" (Action type)
    timezone: str = "America/Los_Angeles"  # 클라이언트 타임존 (Client IANA timezone)
    location: dict | None = None  # GPS 위치 — {lat, lng}, 선택 (Optional GPS location)


class AttendanceResponse(BaseModel):
    """근태 기록 응답 스키마.

    Attendance record response schema with resolved names.

    Attributes:
        id: 근태 UUID (Attendance unique identifier)
        store_id: 매장 UUID (Store identifier)
        store_name: 매장 이름 (Resolved store name)
        user_id: 사용자 UUID (User identifier)
        user_name: 사용자 이름 (Resolved user name)
        work_date: 근무 날짜 (Work date)
        clock_in: 출근 시각 (Clock-in timestamp)
        clock_in_timezone: 출근 타임존 (Timezone at clock-in)
        break_start: 휴식 시작 (Break start timestamp)
        break_end: 휴식 종료 (Break end timestamp)
        clock_out: 퇴근 시각 (Clock-out timestamp)
        clock_out_timezone: 퇴근 타임존 (Timezone at clock-out)
        status: 상태 (Status: clocked_in, on_break, clocked_out)
        total_work_minutes: 총 근무 시간(분) (Total work minutes)
        total_break_minutes: 총 휴식 시간(분) (Total break minutes)
        note: 메모 (Note)
        created_at: 생성 일시 (Creation timestamp)
    """

    id: str  # 근태 UUID 문자열 (Attendance UUID as string)
    store_id: str  # 매장 UUID 문자열 (Store UUID as string)
    store_name: str | None = None  # 매장 이름 — 조인된 값 (Store name, resolved)
    user_id: str  # 사용자 UUID 문자열 (User UUID as string)
    user_name: str | None = None  # 사용자 이름 — 조인된 값 (User name, resolved)
    work_date: date  # 근무 날짜 (Work date)
    clock_in: datetime | None  # 출근 시각 (Clock-in timestamp)
    clock_in_timezone: str | None  # 출근 타임존 (Clock-in timezone)
    break_start: datetime | None  # 휴식 시작 (Break start)
    break_end: datetime | None  # 휴식 종료 (Break end)
    clock_out: datetime | None  # 퇴근 시각 (Clock-out timestamp)
    clock_out_timezone: str | None  # 퇴근 타임존 (Clock-out timezone)
    status: str  # 상태 — "clocked_in"|"on_break"|"clocked_out" (Attendance status)
    total_work_minutes: int | None  # 총 근무 시간(분) (Total work minutes)
    total_break_minutes: int | None  # 총 휴식 시간(분) (Total break minutes)
    note: str | None  # 메모 (Note, may be null)
    created_at: datetime  # 생성 일시 UTC (Creation timestamp)


class AttendanceCorrectionRequest(BaseModel):
    """근태 수정 요청 스키마.

    Attendance correction request schema.
    Used by admins to correct a specific field of an attendance record.

    Attributes:
        field_name: 수정할 필드 이름 (Field to correct)
        corrected_value: 수정 값 — ISO 날짜/시간 문자열 (Corrected value as ISO datetime)
        reason: 수정 사유 (Reason for correction)
    """

    field_name: str  # 수정 필드 — "clock_in"|"clock_out"|"break_start"|"break_end" (Field to correct)
    corrected_value: str  # 수정 값 — ISO datetime 문자열 (Corrected value, ISO format)
    reason: str  # 수정 사유 (Reason for correction)


class AttendanceCorrectionResponse(BaseModel):
    """근태 수정 이력 응답 스키마.

    Attendance correction response schema.

    Attributes:
        id: 수정 이력 UUID (Correction unique identifier)
        field_name: 수정된 필드 (Corrected field name)
        original_value: 수정 전 값 (Original value)
        corrected_value: 수정 후 값 (Corrected value)
        reason: 수정 사유 (Reason for correction)
        corrected_by: 수정자 UUID (Corrector user identifier)
        corrected_by_name: 수정자 이름 (Resolved corrector name)
        created_at: 수정 일시 (Correction timestamp)
    """

    id: str  # 수정 이력 UUID 문자열 (Correction UUID as string)
    field_name: str  # 수정된 필드 (Corrected field name)
    original_value: str | None  # 수정 전 값 (Original value, may be null)
    corrected_value: str  # 수정 후 값 (Corrected value)
    reason: str  # 수정 사유 (Reason for correction)
    corrected_by: str  # 수정자 UUID 문자열 (Corrector UUID as string)
    corrected_by_name: str | None = None  # 수정자 이름 — 조인된 값 (Corrector name, resolved)
    created_at: datetime  # 수정 일시 UTC (Correction timestamp)


class ScheduleResponse(BaseModel):
    """스케줄 응답 스키마 — 관련 엔티티 이름 포함.

    Schedule response schema with resolved entity names.
    Used for both list and detail views.

    Attributes:
        id: 스케줄 UUID (Schedule unique identifier)
        organization_id: 조직 UUID (Organization identifier)
        store_id: 매장 UUID (Store identifier)
        store_name: 매장 이름 (Resolved store name)
        user_id: 사용자 UUID (Worker identifier)
        user_name: 사용자 이름 (Resolved worker name)
        shift_id: 시간대 UUID, nullable (Shift identifier)
        shift_name: 시간대 이름, nullable (Resolved shift name)
        position_id: 포지션 UUID, nullable (Position identifier)
        position_name: 포지션 이름, nullable (Resolved position name)
        work_date: 근무 날짜 (Scheduled work date)
        start_time: 시작 시각 문자열, nullable (Start time)
        end_time: 종료 시각 문자열, nullable (End time)
        status: 상태 (Status: draft/pending/approved/cancelled)
        note: 메모, nullable (Note)
        created_by: 작성자 UUID, nullable (Creator identifier)
        created_by_name: 작성자 이름, nullable (Resolved creator name)
        approved_by: 승인자 UUID, nullable (Approver identifier)
        approved_by_name: 승인자 이름, nullable (Resolved approver name)
        approved_at: 승인 일시, nullable (Approval timestamp)
        work_assignment_id: 생성된 배정 UUID, nullable (Linked work assignment)
        created_at: 생성 일시 (Creation timestamp)
    """

    id: str  # 스케줄 UUID 문자열 (Schedule UUID as string)
    organization_id: str  # 조직 UUID 문자열 (Organization UUID as string)
    store_id: str  # 매장 UUID 문자열 (Store UUID as string)
    store_name: str  # 매장 이름 — 조인된 값 (Store name, resolved)
    user_id: str  # 사용자 UUID 문자열 (Worker UUID as string)
    user_name: str  # 사용자 이름 — 조인된 값 (Worker name, resolved)
    shift_id: str | None  # 시간대 UUID, nullable (Shift UUID as string)
    shift_name: str | None  # 시간대 이름, nullable (Shift name, resolved)
    position_id: str | None  # 포지션 UUID, nullable (Position UUID as string)
    position_name: str | None  # 포지션 이름, nullable (Position name, resolved)
    work_date: date  # 근무 날짜 (Work date)
    start_time: str | None  # 시작 시각 문자열, nullable (Start time as string)
    end_time: str | None  # 종료 시각 문자열, nullable (End time as string)
    status: str  # 상태 — "draft"|"pending"|"approved"|"cancelled" (Schedule status)
    note: str | None  # 메모 (Note, may be null)
    created_by: str | None  # 작성자 UUID, nullable (Creator UUID as string)
    created_by_name: str | None  # 작성자 이름, nullable (Creator name, resolved)
    approved_by: str | None  # 승인자 UUID, nullable (Approver UUID as string)
    approved_by_name: str | None  # 승인자 이름, nullable (Approver name, resolved)
    approved_at: datetime | None  # 승인 일시, nullable (Approval timestamp)
    work_assignment_id: str | None  # 배정 UUID, nullable (Work assignment UUID as string)
    created_at: datetime  # 생성 일시 UTC (Creation timestamp)
