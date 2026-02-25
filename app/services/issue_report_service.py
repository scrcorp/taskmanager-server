"""이슈 리포트 서비스.

Issue report service — Business logic for issue report CRUD.
"""

from datetime import datetime, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import IssueReport
from app.models.user import User
from app.repositories.issue_report_repository import issue_report_repository
from app.schemas.issue_report import IssueReportCreate, IssueReportUpdate
from app.utils.exceptions import NotFoundError


class IssueReportService:

    async def build_response(self, db: AsyncSession, report: IssueReport) -> dict:
        creator_result = await db.execute(
            select(User.full_name).where(User.id == report.created_by)
        )
        created_by_name: str = creator_result.scalar() or "Unknown"

        resolved_by_name: str | None = None
        if report.resolved_by:
            r = await db.execute(
                select(User.full_name).where(User.id == report.resolved_by)
            )
            resolved_by_name = r.scalar()

        return {
            "id": str(report.id),
            "title": report.title,
            "description": report.description,
            "category": report.category,
            "status": report.status,
            "priority": report.priority,
            "store_id": str(report.store_id) if report.store_id else None,
            "created_by": str(report.created_by),
            "created_by_name": created_by_name,
            "resolved_by": str(report.resolved_by) if report.resolved_by else None,
            "resolved_by_name": resolved_by_name,
            "resolved_at": report.resolved_at,
            "created_at": report.created_at,
            "updated_at": report.updated_at,
        }

    # --- Admin ---

    async def list_reports(
        self,
        db: AsyncSession,
        organization_id: UUID,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[IssueReport], int]:
        return await issue_report_repository.get_by_org(
            db, organization_id, status, page, per_page
        )

    async def get_detail(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
    ) -> IssueReport:
        report = await issue_report_repository.get_by_id(db, report_id, organization_id)
        if report is None:
            raise NotFoundError("이슈 리포트를 찾을 수 없습니다 (Issue report not found)")
        return report

    async def create_report(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: IssueReportCreate,
        created_by: UUID,
    ) -> IssueReport:
        store_id = UUID(data.store_id) if data.store_id else None
        return await issue_report_repository.create(
            db,
            {
                "organization_id": organization_id,
                "store_id": store_id,
                "title": data.title,
                "description": data.description,
                "category": data.category,
                "priority": data.priority,
                "created_by": created_by,
            },
        )

    async def update_report(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
        data: IssueReportUpdate,
        current_user_id: UUID,
    ) -> IssueReport:
        update_data = data.model_dump(exclude_unset=True)

        # Auto-set resolved_by/resolved_at when status changes to resolved
        if update_data.get("status") == "resolved":
            update_data["resolved_by"] = current_user_id
            update_data["resolved_at"] = datetime.now(timezone.utc)

        if "store_id" in update_data:
            val = update_data["store_id"]
            update_data["store_id"] = UUID(val) if val else None

        updated = await issue_report_repository.update(
            db, report_id, update_data, organization_id
        )
        if updated is None:
            raise NotFoundError("이슈 리포트를 찾을 수 없습니다 (Issue report not found)")
        return updated

    async def delete_report(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
    ) -> bool:
        deleted = await issue_report_repository.delete(db, report_id, organization_id)
        if not deleted:
            raise NotFoundError("이슈 리포트를 찾을 수 없습니다 (Issue report not found)")
        return deleted

    # --- App (사용자용) ---

    async def list_for_user(
        self,
        db: AsyncSession,
        organization_id: UUID,
        user_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[IssueReport], int]:
        return await issue_report_repository.get_by_user(
            db, organization_id, user_id, page, per_page
        )


issue_report_service: IssueReportService = IssueReportService()
