"""대시보드 서비스 — 대시보드 집계 비즈니스 로직.

Dashboard Service — Aggregation logic for admin dashboard.
Provides checklist completion rates, attendance summary, and overtime summary.
"""

from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import func, select, case, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assignment import WorkAssignment
from app.models.attendance import Attendance
from app.models.checklist import ChecklistInstance, ChecklistCompletion
from app.models.evaluation import Evaluation
from app.models.organization import LaborLawSetting, Store
from app.models.user import User


class DashboardService:
    """대시보드 서비스.

    Dashboard aggregation service for admin dashboard views.
    """

    async def get_checklist_completion(
        self,
        db: AsyncSession,
        organization_id: UUID,
        date_from: date | None = None,
        date_to: date | None = None,
        store_id: UUID | None = None,
    ) -> dict:
        """체크리스트 완료율 집계."""
        if date_from is None:
            date_from = date.today() - timedelta(days=7)
        if date_to is None:
            date_to = date.today()

        base = (
            select(
                func.count(WorkAssignment.id).label("total_assignments"),
                func.sum(
                    case(
                        (WorkAssignment.status == "completed", 1),
                        else_=0,
                    )
                ).label("completed_assignments"),
            )
            .where(
                WorkAssignment.organization_id == organization_id,
                WorkAssignment.work_date >= date_from,
                WorkAssignment.work_date <= date_to,
            )
        )
        if store_id:
            base = base.where(WorkAssignment.store_id == store_id)

        result = await db.execute(base)
        row = result.one()
        total = row.total_assignments or 0
        completed = row.completed_assignments or 0
        rate = round((completed / total * 100), 1) if total > 0 else 0

        return {
            "date_from": str(date_from),
            "date_to": str(date_to),
            "total_assignments": total,
            "completed_assignments": completed,
            "completion_rate": rate,
        }

    async def get_attendance_summary(
        self,
        db: AsyncSession,
        organization_id: UUID,
        date_from: date | None = None,
        date_to: date | None = None,
        store_id: UUID | None = None,
    ) -> dict:
        """근태 요약 집계."""
        if date_from is None:
            date_from = date.today() - timedelta(days=7)
        if date_to is None:
            date_to = date.today()

        base = (
            select(
                func.count(Attendance.id).label("total"),
                func.sum(
                    case((Attendance.status == "completed", 1), else_=0)
                ).label("completed"),
                func.sum(
                    case((Attendance.status == "clocked_in", 1), else_=0)
                ).label("clocked_in"),
                func.avg(Attendance.total_work_minutes).label("avg_work_minutes"),
            )
            .where(
                Attendance.organization_id == organization_id,
                Attendance.work_date >= date_from,
                Attendance.work_date <= date_to,
            )
        )
        if store_id:
            base = base.where(Attendance.store_id == store_id)

        result = await db.execute(base)
        row = result.one()

        return {
            "date_from": str(date_from),
            "date_to": str(date_to),
            "total_records": row.total or 0,
            "completed": row.completed or 0,
            "clocked_in": row.clocked_in or 0,
            "avg_work_minutes": round(float(row.avg_work_minutes or 0), 1),
        }

    async def get_overtime_summary(
        self,
        db: AsyncSession,
        organization_id: UUID,
        week_date: date | None = None,
        store_id: UUID | None = None,
    ) -> dict:
        """초과근무 현황 요약."""
        target_date = week_date or date.today()
        weekday = target_date.weekday()
        week_start = target_date - timedelta(days=weekday)
        week_end = week_start + timedelta(days=6)

        # 노동법 기준
        max_weekly = 40
        law_result = await db.execute(
            select(LaborLawSetting)
            .where(LaborLawSetting.organization_id == organization_id)
            .limit(1)
        )
        law = law_result.scalar_one_or_none()
        if law:
            max_weekly = law.store_max_weekly or law.state_max_weekly or law.federal_max_weekly

        query = (
            select(
                Attendance.user_id,
                func.sum(Attendance.total_work_minutes).label("total_minutes"),
            )
            .where(
                Attendance.organization_id == organization_id,
                Attendance.work_date >= week_start,
                Attendance.work_date <= week_end,
            )
            .group_by(Attendance.user_id)
        )
        if store_id:
            query = query.where(Attendance.store_id == store_id)

        result = await db.execute(query)
        rows = result.all()

        total_users = len(rows)
        overtime_users = 0
        total_overtime_hours = 0.0

        for row in rows:
            total_hours = (row.total_minutes or 0) / 60
            if total_hours > max_weekly:
                overtime_users += 1
                total_overtime_hours += total_hours - max_weekly

        return {
            "week_start": str(week_start),
            "week_end": str(week_end),
            "max_weekly_hours": max_weekly,
            "total_users_with_attendance": total_users,
            "overtime_users": overtime_users,
            "total_overtime_hours": round(total_overtime_hours, 1),
        }

    async def get_evaluation_summary(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> dict:
        """평가 요약."""
        result = await db.execute(
            select(
                func.count(Evaluation.id).label("total"),
                func.sum(case((Evaluation.status == "draft", 1), else_=0)).label("draft"),
                func.sum(case((Evaluation.status == "submitted", 1), else_=0)).label("submitted"),
            )
            .where(Evaluation.organization_id == organization_id)
        )
        row = result.one()
        return {
            "total_evaluations": row.total or 0,
            "draft": row.draft or 0,
            "submitted": row.submitted or 0,
        }


# 싱글턴 인스턴스
dashboard_service: DashboardService = DashboardService()
