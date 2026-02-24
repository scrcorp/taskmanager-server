"""SQLAlchemy ORM 모델 패키지 — 모든 도메인 모델의 중앙 임포트 지점.

SQLAlchemy ORM models package — Central import point for all domain models.
Importing from this package ensures all models are registered with the
SQLAlchemy metadata, which is required for Alembic migrations and
relationship resolution.

Modules:
    organization: 조직 및 매장 (Organization and Store)
    user: 역할 및 사용자 (Role and User)
    work: 근무 시간대 및 포지션 (Shift and Position)
    token: 리프레시 토큰 (Refresh tokens)
    user_store: 사용자-매장 매핑 (User-Store association)
    checklist: 체크리스트 템플릿 (Checklist templates and items)
    assignment: 근무 배정 (Work assignments with JSONB snapshots)
    communication: 공지사항, 추가 업무, 업무 증빙 (Announcements, additional tasks, and task evidences)
    notification: 알림 (User notifications)
    schedule: 스케줄 및 승인 (Schedules and approvals)
    attendance: 근태 관리 — QR 코드, 근태 기록, 수정 이력 (Attendance: QR codes, records, corrections)
"""

from app.models.organization import Organization, Store
from app.models.user import Role, User
from app.models.work import Shift, Position
from app.models.token import RefreshToken
from app.models.user_store import UserStore
from app.models.checklist import ChecklistTemplate, ChecklistTemplateItem, ChecklistInstance, ChecklistCompletion
from app.models.assignment import WorkAssignment
from app.models.communication import Announcement, AdditionalTask, AdditionalTaskAssignee, TaskEvidence
from app.models.notification import Notification
from app.models.schedule import Schedule, ScheduleApproval
from app.models.attendance import QRCode, Attendance, AttendanceCorrection

__all__ = [
    "Organization", "Store",
    "Role", "User",
    "Shift", "Position",
    "RefreshToken", "UserStore",
    "ChecklistTemplate", "ChecklistTemplateItem", "ChecklistInstance", "ChecklistCompletion",
    "WorkAssignment",
    "Announcement", "AdditionalTask", "AdditionalTaskAssignee", "TaskEvidence",
    "Notification",
    "Schedule", "ScheduleApproval",
    "QRCode", "Attendance", "AttendanceCorrection",
]
