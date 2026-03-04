from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import select
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
        """조직에 기본 일일 리포트 템플릿을 생성합니다. 조직 생성 시 호출."""
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
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        is_active: bool | None = None,
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
            .where(
                DailyReportTemplate.id == template_id,
                DailyReportTemplate.organization_id == organization_id,
            )
        )
        result = await db.execute(query)
        template = result.scalar_one_or_none()
        if not template:
            raise NotFoundError("템플릿을 찾을 수 없습니다")
        return template

    async def create_template(
        self, db: AsyncSession, organization_id: UUID, data: DailyReportTemplateCreate
    ) -> DailyReportTemplate:
        template = DailyReportTemplate(
            organization_id=organization_id,
            store_id=UUID(data.store_id) if data.store_id else None,
            name=data.name,
            is_default=data.is_default,
        )
        db.add(template)
        await db.flush()

        for s in data.sections:
            section = DailyReportTemplateSection(
                template_id=template.id,
                title=s.title,
                description=s.description,
                sort_order=s.sort_order,
                is_required=s.is_required,
            )
            db.add(section)
        await db.flush()
        await db.refresh(template)
        # Eager load sections
        query = (
            select(DailyReportTemplate)
            .options(selectinload(DailyReportTemplate.sections))
            .where(DailyReportTemplate.id == template.id)
        )
        result = await db.execute(query)
        return result.scalar_one()

    async def update_template(
        self, db: AsyncSession, template_id: UUID, organization_id: UUID, data: DailyReportTemplateUpdate
    ) -> DailyReportTemplate:
        template = await self.get_template_detail(db, template_id, organization_id)

        if data.name is not None:
            template.name = data.name
        if data.is_default is not None:
            template.is_default = data.is_default
        if data.is_active is not None:
            template.is_active = data.is_active

        # Replace sections if provided
        if data.sections is not None:
            # Delete old sections
            for old_section in list(template.sections):
                await db.delete(old_section)
            await db.flush()
            # Create new sections
            for s in data.sections:
                section = DailyReportTemplateSection(
                    template_id=template.id,
                    title=s.title,
                    description=s.description,
                    sort_order=s.sort_order,
                    is_required=s.is_required,
                )
                db.add(section)
            await db.flush()

        await db.refresh(template)
        # Eager load sections
        query = (
            select(DailyReportTemplate)
            .options(selectinload(DailyReportTemplate.sections))
            .where(DailyReportTemplate.id == template.id)
        )
        result = await db.execute(query)
        return result.scalar_one()

    async def delete_template(
        self, db: AsyncSession, template_id: UUID, organization_id: UUID
    ) -> None:
        template = await self.get_template_detail(db, template_id, organization_id)
        await db.delete(template)
        await db.flush()

    # --- Report Delete ---

    async def delete_report(
        self, db: AsyncSession, report_id: UUID, organization_id: UUID
    ) -> None:
        report = await daily_report_repository.get_with_details(db, report_id, organization_id)
        if not report:
            raise NotFoundError("일일 보고서를 찾을 수 없습니다")
        await db.delete(report)
        await db.flush()

    # --- Existing methods ---

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
        exclude_draft: bool = True,
        page: int = 1,
        per_page: int = 20,
    ):
        # If no explicit status filter, exclude drafts by default
        exclude_status = "draft" if (status is None and exclude_draft) else None
        return await daily_report_repository.get_by_org(
            db, organization_id, store_id=store_id, author_id=author_id,
            date_from=date_from, date_to=date_to, period=period, status=status,
            exclude_status=exclude_status,
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

        # Check duplicate — return existing report ID in error
        existing = await daily_report_repository.find_duplicate(db, store_id, report_date, data.period)
        if existing:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "해당 매장/날짜/시간대에 이미 보고서가 존재합니다",
                    "existing_report_id": str(existing.id),
                    "status": existing.status,
                },
            )

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

        # Comment count (comments may be eager-loaded from list query)
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
