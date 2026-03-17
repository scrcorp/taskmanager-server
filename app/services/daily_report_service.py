from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.models.daily_report import (
    DailyReport,
    DailyReportComment,
    DailyReportSection,
    DailyReportTemplate,
    DailyReportTemplateSection,
)
from app.models.organization import Store
from app.models.user import User
from app.repositories.daily_report_repository import (
    daily_report_repository,
    daily_report_template_repository,
)
from app.schemas.daily_report import (
    DailyReportCommentCreate,
    DailyReportCreate,
    DailyReportTemplateCreate,
    DailyReportTemplateUpdate,
    DailyReportUpdate,
)
from fastapi import HTTPException
from app.utils.exceptions import BadRequestError, DuplicateError, ForbiddenError, NotFoundError


class DailyReportService:

    # --- Default Template for New Orgs ---

    async def create_default_template_for_org(self, db: AsyncSession, organization_id: UUID) -> DailyReportTemplate:
        import json
        from pathlib import Path

        config_path = Path(__file__).resolve().parent.parent.parent / "static" / "default_daily_report_template.json"
        with open(config_path) as f:
            config = json.load(f)

        template = DailyReportTemplate(
            organization_id=organization_id,
            store_id=None,
            name=config["name"],
            is_default=True,
            is_active=True,
        )
        db.add(template)
        await db.flush()

        for s in config["sections"]:
            section = DailyReportTemplateSection(
                template_id=template.id,
                title=s["title"],
                description=s["description"],
                sort_order=s["sort_order"],
                is_required=s["is_required"],
            )
            db.add(section)
        await db.flush()
        return template

    # --- Template CRUD ---

    async def list_templates(
        self, db: AsyncSession, organization_id: UUID,
        store_id: UUID | None = None, is_active: bool | None = None,
    ) -> list[DailyReportTemplate]:
        query = (
            select(DailyReportTemplate)
            .options(selectinload(DailyReportTemplate.sections))
            .where(DailyReportTemplate.organization_id == organization_id)
        )
        if store_id is not None:
            query = query.where(DailyReportTemplate.store_id == store_id)
        if is_active is not None:
            query = query.where(DailyReportTemplate.is_active == is_active)
        query = query.order_by(DailyReportTemplate.created_at.desc())
        result = await db.execute(query)
        return list(result.scalars().all())

    async def get_template_detail(
        self, db: AsyncSession, template_id: UUID, organization_id: UUID
    ) -> DailyReportTemplate:
        query = (
            select(DailyReportTemplate)
            .options(selectinload(DailyReportTemplate.sections))
            .where(DailyReportTemplate.id == template_id, DailyReportTemplate.organization_id == organization_id)
        )
        result = await db.execute(query)
        template = result.scalar_one_or_none()
        if not template:
            raise NotFoundError("Template not found")
        return template

    async def create_template(
        self, db: AsyncSession, organization_id: UUID, data: DailyReportTemplateCreate
    ) -> DailyReportTemplate:
        try:
            template = DailyReportTemplate(
                organization_id=organization_id,
                store_id=UUID(data.store_id) if data.store_id else None,
                name=data.name, is_default=data.is_default,
            )
            db.add(template)
            await db.flush()
            for idx, s in enumerate(data.sections, start=1):
                db.add(DailyReportTemplateSection(
                    template_id=template.id, title=s.title, description=s.description,
                    sort_order=idx, is_required=s.is_required,
                ))
            await db.flush()
            result = await self.get_template_detail(db, template.id, organization_id)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def update_template(
        self, db: AsyncSession, template_id: UUID, organization_id: UUID, data: DailyReportTemplateUpdate
    ) -> DailyReportTemplate:
        try:
            template = await self.get_template_detail(db, template_id, organization_id)
            if data.name is not None:
                template.name = data.name
            if data.is_default is not None:
                template.is_default = data.is_default
            if data.is_active is not None:
                template.is_active = data.is_active
            if data.sections is not None:
                for old in list(template.sections):
                    await db.delete(old)
                await db.flush()
                for idx, s in enumerate(data.sections, start=1):
                    db.add(DailyReportTemplateSection(
                        template_id=template.id, title=s.title, description=s.description,
                        sort_order=idx, is_required=s.is_required,
                    ))
                await db.flush()
            result = await self.get_template_detail(db, template.id, organization_id)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def delete_template(
        self, db: AsyncSession, template_id: UUID, organization_id: UUID
    ) -> None:
        template = await self.get_template_detail(db, template_id, organization_id)
        count_result = await db.execute(
            select(func.count()).where(
                DailyReportTemplate.organization_id == organization_id,
                DailyReportTemplate.is_active == True,
            )
        )
        if (count_result.scalar() or 0) <= 1 and template.is_active:
            raise BadRequestError("Cannot delete the last active template.")
        try:
            await db.delete(template)
            await db.flush()
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    # --- Report CRUD ---

    async def get_template(self, db: AsyncSession, organization_id: UUID, store_id: UUID | None = None):
        template = await daily_report_template_repository.get_template_for_store(db, organization_id, store_id)
        if not template:
            raise NotFoundError("No available report template")
        return template

    async def list_reports(
        self, db: AsyncSession, organization_id: UUID,
        store_id: UUID | None = None, author_id: UUID | None = None,
        date_from: date | None = None, date_to: date | None = None,
        period: str | None = None, status: str | None = None,
        exclude_draft: bool = True, page: int = 1, per_page: int = 20,
    ):
        exclude_status = "draft" if (status is None and exclude_draft) else None
        return await daily_report_repository.get_by_org(
            db, organization_id, store_id=store_id, author_id=author_id,
            date_from=date_from, date_to=date_to, period=period, status=status,
            exclude_status=exclude_status, page=page, per_page=per_page,
        )

    async def get_report(self, db: AsyncSession, report_id: UUID, organization_id: UUID):
        report = await daily_report_repository.get_with_details(db, report_id, organization_id)
        if not report:
            raise NotFoundError("Daily report not found")
        return report

    async def create_report(
        self, db: AsyncSession, organization_id: UUID, author_id: UUID, data: DailyReportCreate
    ):
        store_id = UUID(data.store_id)
        report_date = date.fromisoformat(data.report_date)

        existing = await daily_report_repository.find_duplicate(db, store_id, report_date, data.period)
        if existing:
            raise HTTPException(status_code=409, detail={
                "message": "A report already exists for this store/date/period",
                "existing_report_id": str(existing.id),
                "status": existing.status,
            })

        template_id = UUID(data.template_id) if data.template_id else None
        if template_id:
            template = await daily_report_template_repository.get_by_id_with_sections(db, template_id)
        else:
            template = await daily_report_template_repository.get_template_for_store(db, organization_id, store_id)
        if not template:
            raise NotFoundError("No available report template")

        try:
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

            # Create DailyReportSection rows from template sections
            for ts in template.sections:
                db.add(DailyReportSection(
                    report_id=report.id,
                    template_section_id=ts.id,
                    title=ts.title,
                    content=None,
                    sort_order=ts.sort_order,
                ))
            await db.flush()
            await db.refresh(report)
            await db.commit()
            return report
        except Exception:
            await db.rollback()
            raise

    async def update_report(
        self, db: AsyncSession, report_id: UUID, organization_id: UUID, author_id: UUID, data: DailyReportUpdate
    ):
        report = await self.get_report(db, report_id, organization_id)
        if report.author_id != author_id:
            raise ForbiddenError("Only the author can update this report")
        if report.status != "draft":
            raise BadRequestError("Only draft reports can be updated")

        try:
            # Update content in DailyReportSection rows by sort_order
            update_map = {u.sort_order: u.content for u in data.sections}
            for section in report.sections:
                if section.sort_order in update_map:
                    section.content = update_map[section.sort_order]
            await db.flush()
            await db.refresh(report)
            await db.commit()
            return report
        except Exception:
            await db.rollback()
            raise

    async def submit_report(self, db: AsyncSession, report_id: UUID, organization_id: UUID, author_id: UUID):
        report = await self.get_report(db, report_id, organization_id)
        if report.author_id != author_id:
            raise ForbiddenError("Only the author can submit this report")
        if report.status != "draft":
            raise BadRequestError("Only draft reports can be submitted")
        try:
            report.status = "submitted"
            report.submitted_at = datetime.now(timezone.utc)
            await db.flush()
            await db.refresh(report)
            await db.commit()
            return report
        except Exception:
            await db.rollback()
            raise

    async def delete_report(
        self, db: AsyncSession, report_id: UUID, organization_id: UUID, author_id: UUID | None = None
    ) -> None:
        report = await daily_report_repository.get_with_details(db, report_id, organization_id)
        if not report:
            raise NotFoundError("Daily report not found")
        # If author_id provided, only author can delete drafts
        if author_id:
            if report.author_id != author_id:
                raise ForbiddenError("Only the author can delete this report")
            if report.status != "draft":
                raise BadRequestError("Only draft reports can be deleted")
        try:
            await db.delete(report)
            await db.flush()
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def add_comment(
        self, db: AsyncSession, report_id: UUID, organization_id: UUID, user_id: UUID, data: DailyReportCommentCreate
    ):
        report = await daily_report_repository.get_with_details(db, report_id, organization_id)
        if not report:
            raise NotFoundError("Daily report not found")
        try:
            comment = DailyReportComment(report_id=report.id, user_id=user_id, content=data.content)
            db.add(comment)
            await db.flush()
            await db.refresh(comment)
            await db.commit()
            return comment
        except Exception:
            await db.rollback()
            raise

    def _to_report_dict(
        self,
        report: DailyReport,
        author_name: str,
        store_name: str | None,
        include_details: bool = False,
        comment_user_names: dict | None = None,
    ) -> dict:
        """Build a report response dict from pre-fetched lookup values."""
        try:
            comment_count = len(report.comments)
        except Exception:
            comment_count = 0

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
            "comment_count": comment_count,
        }

        if include_details:
            resp["sections"] = [
                {
                    "id": str(s.id),
                    "title": s.title,
                    "content": s.content,
                    "sort_order": s.sort_order,
                    "template_section_id": str(s.template_section_id) if s.template_section_id else None,
                }
                for s in report.sections
            ]
            names = comment_user_names or {}
            resp["comments"] = [
                {
                    "id": str(c.id),
                    "user_id": str(c.user_id),
                    "user_name": names.get(c.user_id) or "Unknown",
                    "content": c.content,
                    "created_at": c.created_at,
                }
                for c in report.comments
            ]
        else:
            resp["sections"] = []
            resp["comments"] = []

        return resp

    async def build_response(self, db: AsyncSession, report: DailyReport, include_details: bool = False) -> dict:
        user_result = await db.execute(select(User.full_name).where(User.id == report.author_id))
        author_name: str = user_result.scalar() or "Unknown"
        store_result = await db.execute(select(Store.name).where(Store.id == report.store_id))
        store_name: str | None = store_result.scalar()

        comment_user_names: dict | None = None
        if include_details:
            try:
                comment_user_ids = list({c.user_id for c in report.comments})
            except Exception:
                comment_user_ids = []
            if comment_user_ids:
                cu_result = await db.execute(
                    select(User.id, User.full_name).where(User.id.in_(comment_user_ids))
                )
                comment_user_names = {row.id: row.full_name for row in cu_result}

        return self._to_report_dict(report, author_name, store_name, include_details, comment_user_names)

    async def build_responses_batch(self, db: AsyncSession, reports: list[DailyReport]) -> list[dict]:
        """Build response dicts for a list of reports using batch queries."""
        author_ids = list({r.author_id for r in reports})
        store_ids = list({r.store_id for r in reports})

        author_names: dict = {}
        if author_ids:
            result = await db.execute(
                select(User.id, User.full_name).where(User.id.in_(author_ids))
            )
            author_names = {row.id: row.full_name for row in result}

        store_names: dict = {}
        if store_ids:
            result = await db.execute(
                select(Store.id, Store.name).where(Store.id.in_(store_ids))
            )
            store_names = {row.id: row.name for row in result}

        return [
            self._to_report_dict(
                r,
                author_names.get(r.author_id) or "Unknown",
                store_names.get(r.store_id),
            )
            for r in reports
        ]

    async def build_template_response(self, template) -> dict:
        return {
            "id": str(template.id),
            "name": template.name,
            "sections": [
                {"id": str(s.id), "title": s.title, "description": s.description,
                 "sort_order": s.sort_order, "is_required": s.is_required}
                for s in template.sections
            ],
        }


daily_report_service: DailyReportService = DailyReportService()
