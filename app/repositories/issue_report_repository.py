"""이슈 리포트 레포지토리.

Issue report repository — Handles issue_reports DB queries.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import IssueReport
from app.repositories.base import BaseRepository


class IssueReportRepository(BaseRepository[IssueReport]):

    def __init__(self) -> None:
        super().__init__(IssueReport)

    async def get_by_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[IssueReport], int]:
        query: Select = (
            select(IssueReport)
            .where(IssueReport.organization_id == organization_id)
            .order_by(IssueReport.created_at.desc())
        )
        if status:
            query = query.where(IssueReport.status == status)
        return await self.get_paginated(db, query, page, per_page)

    async def get_by_user(
        self,
        db: AsyncSession,
        organization_id: UUID,
        user_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[IssueReport], int]:
        query: Select = (
            select(IssueReport)
            .where(
                IssueReport.organization_id == organization_id,
                IssueReport.created_by == user_id,
            )
            .order_by(IssueReport.created_at.desc())
        )
        return await self.get_paginated(db, query, page, per_page)


issue_report_repository: IssueReportRepository = IssueReportRepository()
