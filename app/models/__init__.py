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
    communication: 공지사항, 추가 업무, 증빙, 읽음추적 (Notices, tasks, evidences, read tracking)
    alert: 알림 (User alerts)
    schedule: 스케줄 및 승인 (Schedules and approvals)
    attendance: 근태 관리 (Attendance: QR codes, records, corrections)
    evaluation: 평가 템플릿, 평가 (Evaluation templates, evaluations — JSONB config/snapshot)
    daily_report: 일일 보고서 템플릿, 보고서, 섹션, 코멘트 (Daily report templates, reports, sections, comments)
"""

from app.models.organization import Organization, Store, ShiftPreset, LaborLawSetting
from app.models.user import Role, User
from app.models.org_member import OrgMember, OrgMemberStore
from app.models.platform_admin import PlatformAdmin
from app.models.employee_no_history import EmployeeNoHistory
from app.models.work import Shift, Position
from app.models.token import RefreshToken
from app.models.user_store import UserStore
from app.models.checklist import ChecklistTemplate, ChecklistTemplateItem, ChecklistInstance, ChecklistInstanceItem, ChecklistItemSubmission, ChecklistItemReviewLog, ChecklistItemMessage
from app.models.communication import Notice, NoticeRead, Voice
from app.models.alert import Alert
from app.models.schedule import Schedule, StoreWorkRole, StoreBreakRule, ScheduleRequestTemplate, ScheduleRequestTemplateItem, ScheduleRequest, ScheduleAuditLog
from app.models.attendance import QRCode, Attendance, AttendanceCorrection
from app.models.attendance_break import AttendanceBreak
from app.models.attendance_device import AttendanceDevice
from app.models.access_code import AccessCode
from app.models.evaluation import EvalTemplate, Evaluation
from app.models.warning import Warning
from app.models.warning_category import WarningCategory
from app.models.warning_signature import WarningSignature
from app.models.permission import Permission, RolePermission
from app.models.daily_report import DailyReportTemplate, DailyReportTemplateSection, DailyReport, DailyReportSection, DailyReportComment
from app.models.report import Report, ReportTemplate, ReportComment, ReportType, ReportAcknowledgement
from app.models.task import Task, TaskAssignee, TaskComment
from app.models.email_verification import EmailVerificationCode
from app.models.inventory import InventoryCategory, InventorySubUnit, InventoryProduct, StoreInventory, InventoryTransaction, InventoryAudit, InventoryAuditItem, InventoryAuditSetting
from app.models.settings import SettingsRegistry, OrgSetting, StoreSetting, StaffSetting
from app.models.hiring import StoreHiringForm, Candidate, Application, CandidateBlock
from app.models.interview import InterviewSlot, InterviewSlotPreference
from app.models.app_version import AppVersion
from app.models.tip import TipEntry, TipDistribution, TipAuditLog, TipPeriod, Form4070Document
from app.models.schedule_report import ScheduleReportSnapshot
from app.models.file import File, FileUsage
from app.models.changelog import ChangelogPost

__all__ = [
    "Organization", "Store", "ShiftPreset", "LaborLawSetting",
    "Role", "User",
    "OrgMember", "OrgMemberStore",
    "PlatformAdmin",
    "EmployeeNoHistory",
    "Shift", "Position",
    "RefreshToken", "UserStore",
    "ChecklistTemplate", "ChecklistTemplateItem", "ChecklistInstance", "ChecklistInstanceItem", "ChecklistItemSubmission", "ChecklistItemReviewLog", "ChecklistItemMessage",
    "Notice", "NoticeRead", "Voice",
    "Alert",
    "Schedule", "StoreWorkRole", "StoreBreakRule", "ScheduleRequestTemplate", "ScheduleRequestTemplateItem", "ScheduleRequest", "ScheduleAuditLog",
    "QRCode", "Attendance", "AttendanceCorrection",
    "AttendanceBreak",
    "AttendanceDevice",
    "AccessCode",
    "EvalTemplate", "Evaluation",
    "Warning",
    "WarningCategory",
    "WarningSignature",
    "Permission", "RolePermission",
    "DailyReportTemplate", "DailyReportTemplateSection", "DailyReport", "DailyReportSection", "DailyReportComment",
    "Report", "ReportTemplate", "ReportComment", "ReportType", "ReportAcknowledgement",
    "Task", "TaskAssignee", "TaskComment",
    "EmailVerificationCode",
    "InventoryCategory", "InventorySubUnit", "InventoryProduct", "StoreInventory", "InventoryTransaction", "InventoryAudit", "InventoryAuditItem", "InventoryAuditSetting",
    "SettingsRegistry", "OrgSetting", "StoreSetting", "StaffSetting",
    "StoreHiringForm", "Candidate", "Application", "CandidateBlock",
    "InterviewSlot", "InterviewSlotPreference",
    "AppVersion",
    "TipEntry", "TipDistribution", "TipAuditLog", "TipPeriod", "Form4070Document",
    "ScheduleReportSnapshot",
    "File", "FileUsage",
    "ChangelogPost",
]
