"""SQLAlchemy ORM 모델 패키지 — 모든 도메인 모델의 중앙 임포트 지점.

SQLAlchemy ORM models package — Central import point for all domain models.
Importing from this package ensures all models are registered with the
SQLAlchemy metadata, which is required for Alembic migrations and
relationship resolution.

Modules:
    organization: 조직 및 브랜드 (Organization and Brand)
    user: 역할 및 사용자 (Role and User)
    work: 근무 시간대 및 포지션 (Shift and Position)
    token: 리프레시 토큰 (Refresh tokens)
    user_brand: 사용자-브랜드 매핑 (User-Brand association)
    checklist: 체크리스트 템플릿 (Checklist templates and items)
    assignment: 근무 배정 (Work assignments with JSONB snapshots)
    communication: 공지사항 및 추가 업무 (Announcements and additional tasks)
    notification: 알림 (User notifications)
"""

from app.models.organization import Organization, Brand
from app.models.user import Role, User
from app.models.work import Shift, Position
from app.models.token import RefreshToken
from app.models.user_brand import UserBrand
from app.models.checklist import ChecklistTemplate, ChecklistTemplateItem
from app.models.assignment import WorkAssignment
from app.models.communication import Announcement, AdditionalTask, AdditionalTaskAssignee
from app.models.notification import Notification

__all__ = [
    "Organization", "Brand",
    "Role", "User",
    "Shift", "Position",
    "RefreshToken", "UserBrand",
    "ChecklistTemplate", "ChecklistTemplateItem",
    "WorkAssignment",
    "Announcement", "AdditionalTask", "AdditionalTaskAssignee",
    "Notification",
]
