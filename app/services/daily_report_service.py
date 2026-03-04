from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.daily_report import DailyReport, DailyReportComment, DailyReportSection
from app.models.organization import Store
from app.models.user import User
from app.repositories.daily_report_repository import (
    daily_report_repository,
    daily_report_template_repository,
)
from app.schemas.daily_report import DailyReportCreate, DailyReportUpdate, DailyReportCommentCreate
from app.utils.exceptions import BadRequestError, DuplicateError, ForbiddenError, NotFoundError


class DailyReportService:

    async def get_template(self, db: AsyncSession, organization_id: UUID, store_id: UUID | None = None):
        template = await daily_report_template_repository.get_template_for_store(db, organization_id, store_id)
        if not template:
            raise NotFoundError("사용 가능한 보고서 템플릿이 없습니다")
        return template

    async def list_reports(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        author_id: UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        period: str | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ):
        return await daily_report_repository.get_by_org(
            db, organization_id, store_id=store_id, author_id=author_id,
            date_from=date_from, date_to=date_to, period=period, status=status,
            page=page, per_page=per_page,
        )

    async def get_report(self, db: AsyncSession, report_id: UUID, organization_id: UUID):
        report = await daily_report_repository.get_with_details(db, report_id, organization_id)
        if not report:
            raise NotFoundError("일일 보고서를 찾을 수 없습니다")
        return report

    async def create_report(
        self, db: AsyncSession, organization_id: UUID, author_id: UUID, data: DailyReportCreate
    ):
        store_id = UUID(data.store_id)
        report_date = date.fromisoformat(data.report_date)

        # Check duplicate
        if await daily_report_repository.check_duplicate(db, store_id, report_date, data.period):
            raise DuplicateError("해당 매장/날짜/시간대에 이미 보고서가 존재합니다")

        # Resolve template
        template_id = UUID(data.template_id) if data.template_id else None
        if template_id:
            template = await daily_report_template_repository.get_by_id_with_sections(db, template_id)
        else:
            template = await daily_report_template_repository.get_template_for_store(db, organization_id, store_id)
        if not template:
            raise NotFoundError("사용 가능한 보고서 템플릿이 없습니다")

        # Create report
        report = DailyReport(
            organization_id=organization_id,
            store_id=store_id,
            template_id=template.id,
            author_id=author_id,
            report_date=report_date,
            period=data.period,
            status="draft",
        )
        db.add(report)
        await db.flush()

        # Snapshot sections from template
        for ts in template.sections:
            section = DailyReportSection(
                report_id=report.id,
                template_section_id=ts.id,
                title=ts.title,
                content=None,
                sort_order=ts.sort_order,
            )
            db.add(section)
        await db.flush()
        await db.refresh(report)
        return report

    async def update_report(
        self, db: AsyncSession, report_id: UUID, organization_id: UUID, author_id: UUID, data: DailyReportUpdate
    ):
        report = await self.get_report(db, report_id, organization_id)
        if report.author_id != author_id:
            raise ForbiddenError("본인이 작성한 보고서만 수정할 수 있습니다")
        if report.status != "draft":
            raise BadRequestError("작성 중인 보고서만 수정할 수 있습니다")

        section_map = {str(s.id): s for s in report.sections}
        for update in data.sections:
            section = section_map.get(update.section_id)
            if section:
                section.content = update.content
        await db.flush()
        await db.refresh(report)
        return report

    async def submit_report(self, db: AsyncSession, report_id: UUID, organization_id: UUID, author_id: UUID):
        report = await self.get_report(db, report_id, organization_id)
        if report.author_id != author_id:
            raise ForbiddenError("본인이 작성한 보고서만 제출할 수 있습니다")
        if report.status != "draft":
            raise BadRequestError("작성 중인 보고서만 제출할 수 있습니다")
        report.status = "submitted"
        report.submitted_at = datetime.now(timezone.utc)
        await db.flush()
        await db.refresh(report)
        return report

    async def add_comment(
        self, db: AsyncSession, report_id: UUID, organization_id: UUID, user_id: UUID, data: DailyReportCommentCreate
    ):
        report = await daily_report_repository.get_with_details(db, report_id, organization_id)
        if not report:
            raise NotFoundError("일일 보고서를 찾을 수 없습니다")
        comment = DailyReportComment(
            report_id=report.id,
            user_id=user_id,
            content=data.content,
        )
        db.add(comment)
        await db.flush()
        await db.refresh(comment)
        return comment

    async def build_response(self, db: AsyncSession, report: DailyReport, include_details: bool = False) -> dict:
        # Resolve author name
        user_result = await db.execute(select(User.full_name).where(User.id == report.author_id))
        author_name = user_result.scalar() or "Unknown"

        # Resolve store name
        store_result = await db.execute(select(Store.name).where(Store.id == report.store_id))
        store_name = store_result.scalar()

        resp = {
            "id": str(report.id),
            "organization_id": str(report.organization_id),
            "store_id": str(report.store_id),
            "store_name": store_name,
            "template_id": str(report.template_id) if report.template_id else None,
            "author_id": str(report.author_id),
            "author_name": author_name,
            "report_date": report.report_date,
            "period": report.period,
            "status": report.status,
            "submitted_at": report.submitted_at,
            "created_at": report.created_at,
            "updated_at": report.updated_at,
        }

        if include_details:
            resp["sections"] = [
                {
                    "id": str(s.id),
                    "template_section_id": str(s.template_section_id) if s.template_section_id else None,
                    "title": s.title,
                    "content": s.content,
                    "sort_order": s.sort_order,
                }
                for s in report.sections
            ]
            # Resolve comment user names
            comments = []
            for c in report.comments:
                cu_result = await db.execute(select(User.full_name).where(User.id == c.user_id))
                cu_name = cu_result.scalar() or "Unknown"
                comments.append({
                    "id": str(c.id),
                    "user_id": str(c.user_id),
                    "user_name": cu_name,
                    "content": c.content,
                    "created_at": c.created_at,
                })
            resp["comments"] = comments
        else:
            resp["sections"] = []
            resp["comments"] = []

        return resp

    async def build_template_response(self, template) -> dict:
        return {
            "id": str(template.id),
            "name": template.name,
            "sections": [
                {
                    "id": str(s.id),
                    "title": s.title,
                    "description": s.description,
                    "sort_order": s.sort_order,
                    "is_required": s.is_required,
                }
                for s in template.sections
            ],
        }


daily_report_service: DailyReportService = DailyReportService()
