"""SQLAlchemy ORM 모델 패키지 — 모든 도메인 모델의 중앙 임포트 지점.

SQLAlchemy ORM models package — Central import point for all domain models.
Importing from this package ensures all models are registered with the
SQLAlchemy metadata, which is required for Alembic migrations and
relationship resolution.

Modules:
    organization: 조직, 매장, 시프트프리셋, 노동법설정 (Organization, Store, ShiftPreset, LaborLawSetting)
    user: 역할 및 사용자 (Role and User)
    work: 근무 시간대 및 포지션 (Shift and Position)
    token: 리프레시 토큰 (Refresh tokens)
    user_store: 사용자-매장 매핑 (User-Store association)
    checklist: 체크리스트 템플릿, 인스턴스, 아이템 (Checklist templates, instances, items)
    communication: 공지사항, 추가 업무, 증빙, 읽음추적 (Announcements, tasks, evidences, read tracking)
    notification: 알림 (User notifications)
    schedule: 스케줄 및 승인 (Schedules and approvals)
    attendance: 근태 관리 (Attendance: QR codes, records, corrections)
    evaluation: 평가 템플릿, 평가, 응답 (Evaluation templates, evaluations, responses)
    daily_report: 일일 보고서 템플릿, 보고서, 섹션, 코멘트 (Daily report templates, reports, sections, comments)
"""

from app.models.organization import Organization, Store, ShiftPreset, LaborLawSetting
from app.models.user import Role, User
from app.models.work import Shift, Position
from app.models.token import RefreshToken
from app.models.user_store import UserStore
from app.models.checklist import ChecklistTemplate, ChecklistTemplateItem, ChecklistInstance, ChecklistInstanceItem, ChecklistItemFile, ChecklistItemSubmission, ChecklistItemReviewLog, ChecklistItemMessage
from app.models.communication import Announcement, AdditionalTask, AdditionalTaskAssignee, TaskEvidence, AnnouncementRead, Voice
from app.models.notification import Notification
from app.models.schedule import Schedule, StoreWorkRole, StoreBreakRule, ScheduleRequestTemplate, ScheduleRequestTemplateItem, ScheduleRequest, ScheduleAuditLog
from app.models.attendance import QRCode, Attendance, AttendanceCorrection
from app.models.evaluation import EvalTemplate, EvalTemplateItem, Evaluation, EvalResponse
from app.models.permission import Permission, RolePermission
from app.models.daily_report import DailyReportTemplate, DailyReportTemplateSection, DailyReport, DailyReportSection, DailyReportComment
from app.models.email_verification import EmailVerificationCode
from app.models.inventory import InventoryCategory, InventorySubUnit, InventoryProduct, StoreInventory, InventoryTransaction, InventoryAudit, InventoryAuditItem, InventoryAuditSetting
from app.models.settings import SettingsRegistry, OrgSetting, StoreSetting, StaffSetting

__all__ = [
    "Organization", "Store", "ShiftPreset", "LaborLawSetting",
    "Role", "User",
    "Shift", "Position",
    "RefreshToken", "UserStore",
    "ChecklistTemplate", "ChecklistTemplateItem", "ChecklistInstance", "ChecklistInstanceItem", "ChecklistItemFile", "ChecklistItemSubmission", "ChecklistItemReviewLog", "ChecklistItemMessage",
    "Announcement", "AdditionalTask", "AdditionalTaskAssignee", "TaskEvidence", "AnnouncementRead", "Voice",
    "Notification",
    "Schedule", "StoreWorkRole", "StoreBreakRule", "ScheduleRequestTemplate", "ScheduleRequestTemplateItem", "ScheduleRequest", "ScheduleAuditLog",
    "QRCode", "Attendance", "AttendanceCorrection",
    "EvalTemplate", "EvalTemplateItem", "Evaluation", "EvalResponse",
    "Permission", "RolePermission",
    "DailyReportTemplate", "DailyReportTemplateSection", "DailyReport", "DailyReportSection", "DailyReportComment",
    "EmailVerificationCode",
    "InventoryCategory", "InventorySubUnit", "InventoryProduct", "StoreInventory", "InventoryTransaction", "InventoryAudit", "InventoryAuditItem", "InventoryAuditSetting",
    "SettingsRegistry", "OrgSetting", "StoreSetting", "StaffSetting",
]
