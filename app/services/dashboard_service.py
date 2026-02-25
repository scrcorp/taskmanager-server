"""대시보드 서비스 — 대시보드 집계 비즈니스 로직.

Dashboard Service — Aggregation logic for admin dashboard.
Provides checklist completion rates, attendance summary, and overtime summary.
"""

from datetime import date, timedelta
from io import BytesIO
from uuid import UUID

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
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


    async def export_excel(
        self,
        db: AsyncSession,
        organization_id: UUID,
        date_from: date | None = None,
        date_to: date | None = None,
        store_id: UUID | None = None,
    ) -> bytes:
        """대시보드 데이터를 Excel 파일로 내보내기."""
        if date_from is None:
            date_from = date.today() - timedelta(days=7)
        if date_to is None:
            date_to = date.today()

        wb = Workbook()
        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill(start_color="2D3436", end_color="2D3436", fill_type="solid")

        def style_headers(ws, headers: list[str]) -> None:
            for col_idx, h in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col_idx, value=h)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center")

        # --- Sheet 1: Checklist Completion ---
        ws1 = wb.active
        ws1.title = "Checklist Completion"
        headers1 = ["Store", "User", "Work Date", "Status", "Total Items", "Completed Items"]
        style_headers(ws1, headers1)

        checklist_query = (
            select(
                Store.name.label("store_name"),
                User.full_name.label("user_name"),
                WorkAssignment.work_date,
                WorkAssignment.status,
                WorkAssignment.total_items,
                WorkAssignment.completed_items,
            )
            .join(Store, WorkAssignment.store_id == Store.id)
            .join(User, WorkAssignment.user_id == User.id)
            .where(
                WorkAssignment.organization_id == organization_id,
                WorkAssignment.work_date >= date_from,
                WorkAssignment.work_date <= date_to,
            )
            .order_by(WorkAssignment.work_date.desc())
        )
        if store_id:
            checklist_query = checklist_query.where(WorkAssignment.store_id == store_id)

        result = await db.execute(checklist_query)
        for row in result.all():
            ws1.append([row.store_name, row.user_name, str(row.work_date), row.status, row.total_items, row.completed_items])

        for i, w in enumerate([20, 20, 15, 15, 12, 15], 1):
            ws1.column_dimensions[ws1.cell(row=1, column=i).column_letter].width = w

        # --- Sheet 2: Attendance ---
        ws2 = wb.create_sheet("Attendance")
        headers2 = ["Store", "User", "Work Date", "Clock In", "Clock Out", "Break (min)", "Work (min)", "Status"]
        style_headers(ws2, headers2)

        att_query = (
            select(
                Store.name.label("store_name"),
                User.full_name.label("user_name"),
                Attendance.work_date,
                Attendance.clock_in,
                Attendance.clock_out,
                Attendance.total_break_minutes,
                Attendance.total_work_minutes,
                Attendance.status,
            )
            .join(Store, Attendance.store_id == Store.id)
            .join(User, Attendance.user_id == User.id)
            .where(
                Attendance.organization_id == organization_id,
                Attendance.work_date >= date_from,
                Attendance.work_date <= date_to,
            )
            .order_by(Attendance.work_date.desc())
        )
        if store_id:
            att_query = att_query.where(Attendance.store_id == store_id)

        result = await db.execute(att_query)
        for row in result.all():
            ws2.append([
                row.store_name,
                row.user_name,
                str(row.work_date),
                row.clock_in.isoformat() if row.clock_in else "",
                row.clock_out.isoformat() if row.clock_out else "",
                row.total_break_minutes or 0,
                row.total_work_minutes or 0,
                row.status,
            ])

        for i, w in enumerate([20, 20, 15, 22, 22, 12, 12, 15], 1):
            ws2.column_dimensions[ws2.cell(row=1, column=i).column_letter].width = w

        # --- Sheet 3: Overtime ---
        ws3 = wb.create_sheet("Overtime")
        headers3 = ["User", "Week Start", "Week End", "Total Hours", "Max Weekly", "Overtime Hours"]
        style_headers(ws3, headers3)

        # Calculate weekly overtime for the date range
        # Use Monday of date_from week to Sunday of date_to week
        weekday = date_from.weekday()
        week_start = date_from - timedelta(days=weekday)
        week_end_of_range = date_to + timedelta(days=(6 - date_to.weekday()))

        max_weekly = 40
        law_result = await db.execute(
            select(LaborLawSetting)
            .where(LaborLawSetting.organization_id == organization_id)
            .limit(1)
        )
        law = law_result.scalar_one_or_none()
        if law:
            max_weekly = law.store_max_weekly or law.state_max_weekly or law.federal_max_weekly

        ot_query = (
            select(
                Attendance.user_id,
                func.sum(Attendance.total_work_minutes).label("total_minutes"),
            )
            .where(
                Attendance.organization_id == organization_id,
                Attendance.work_date >= week_start,
                Attendance.work_date <= week_end_of_range,
            )
            .group_by(Attendance.user_id)
        )
        if store_id:
            ot_query = ot_query.where(Attendance.store_id == store_id)

        result = await db.execute(ot_query)
        for row in result.all():
            total_hours = (row.total_minutes or 0) / 60
            overtime = max(0, total_hours - max_weekly)
            user_result = await db.execute(
                select(User.full_name).where(User.id == row.user_id)
            )
            user_name = user_result.scalar() or "Unknown"
            ws3.append([
                user_name,
                str(week_start),
                str(week_end_of_range),
                round(total_hours, 1),
                max_weekly,
                round(overtime, 1),
            ])

        for i, w in enumerate([20, 15, 15, 12, 12, 15], 1):
            ws3.column_dimensions[ws3.cell(row=1, column=i).column_letter].width = w

        buffer = BytesIO()
        wb.save(buffer)
        return buffer.getvalue()


# 싱글턴 인스턴스
dashboard_service: DashboardService = DashboardService()
