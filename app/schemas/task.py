"""Task schemas (renamed from issues, originally additional_tasks)."""
from datetime import datetime
from pydantic import BaseModel


TASK_STATUSES = ["pending", "in_progress", "under_review", "completed"]
TASK_PRIORITIES = ["normal", "urgent"]
TASK_SEVERITIES = ["low", "medium", "high", "critical"]


class TaskAttachment(BaseModel):
    """첨부 파일 — issue report attachments 와 동일 shape."""
    key: str
    url: str | None = None
    mime_type: str | None = None
    kind: str | None = None  # "image" | "video" | "file"
    name: str | None = None
    size: int | None = None


class TaskLinks(BaseModel):
    schedule_ids: list[str] = []
    checklist_instance_ids: list[str] = []
    position_ids: list[str] = []
    work_role_ids: list[str] = []
    related_user_ids: list[str] = []
    related_roles: list[str] = []


class TaskCreate(BaseModel):
    # store scope — 빈 list = org-wide, 1개 = single store, 여러개 = multi-store.
    # 기존 store_id 도 호환 위해 받지만 store_ids 가 비어있을 때만 사용.
    store_ids: list[str] = []
    store_id: str | None = None  # legacy, deprecated
    title: str
    description: str | None = None
    priority: str = "normal"
    severity: str | None = None
    category: str | None = None
    due_date: datetime | None = None
    assignee_ids: list[str] = []
    source_report_id: str | None = None
    links: TaskLinks | None = None
    attachments: list[TaskAttachment] = []


class TaskUpdate(BaseModel):
    store_ids: list[str] | None = None
    title: str | None = None
    description: str | None = None
    priority: str | None = None
    severity: str | None = None
    category: str | None = None
    status: str | None = None
    due_date: datetime | None = None
    assignee_ids: list[str] | None = None
    links: TaskLinks | None = None
    attachments: list[TaskAttachment] | None = None


class TaskTransitionRequest(BaseModel):
    """status 전이 + 선택적 코멘트(텍스트 + 첨부).

    허용 전이:
      - pending → in_progress (assignee)
      - in_progress → under_review (assignee, "제출" — 텍스트 + 사진/동영상/파일)
      - under_review → completed (manager, "승인")
      - under_review → in_progress (manager, "반려" — 코멘트 권장)
      - completed → in_progress (manager, "reopen")
    """
    status: str
    comment: str | None = None
    attachments: list[TaskAttachment] = []


class TaskCommentCreate(BaseModel):
    content: str
    attachments: list[TaskAttachment] = []


class TaskCommentOut(BaseModel):
    id: str
    task_id: str
    user_id: str | None = None
    user_name: str | None = None
    content: str
    kind: str  # "comment" | "system"
    attachments: list[TaskAttachment] = []
    created_at: datetime


class TaskAssigneeOut(BaseModel):
    user_id: str | None = None
    user_name: str | None = None


class TaskResponse(BaseModel):
    id: str
    organization_id: str
    # store scope
    store_ids: list[str] = []           # [] = org-wide
    store_names: list[str] = []         # 표시용 (store_ids 순서)
    # legacy mirror — store_ids[0] (or null)
    store_id: str | None = None
    store_name: str | None = None
    title: str
    description: str | None = None
    priority: str
    severity: str | None = None
    category: str | None = None
    status: str
    due_date: datetime | None = None
    created_by: str | None = None
    created_by_name: str | None = None
    source_report_id: str | None = None
    links: TaskLinks | None = None
    attachments: list[TaskAttachment] = []
    submitted_at: datetime | None = None
    submitted_by: str | None = None
    submitted_by_name: str | None = None
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_by_name: str | None = None
    created_at: datetime
    updated_at: datetime
    assignees: list[TaskAssigneeOut] = []


class TaskPromoteRequest(BaseModel):
    """Issue report에서 Task로 promote할 때 사용할 옵션."""
    title: str | None = None
    description: str | None = None
    priority: str = "normal"
    severity: str | None = None
    category: str | None = None
    due_date: datetime | None = None
    assignee_ids: list[str] = []
