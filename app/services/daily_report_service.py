from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.daily_report import (
    DailyReport,
    DailyReportComment,
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
            raise NotFoundError("템플릿을 찾을 수 없습니다")
        return template

    async def create_template(
        self, db: AsyncSession, organization_id: UUID, data: DailyReportTemplateCreate
    ) -> DailyReportTemplate:
        template = DailyReportTemplate(
            organization_id=organization_id,
            store_id=UUID(data.store_id) if data.store_id else None,
            name=data.name, is_default=data.is_default,
        )
        db.add(template)
        await db.flush()
        for s in data.sections:
            db.add(DailyReportTemplateSection(
                template_id=template.id, title=s.title, description=s.description,
                sort_order=s.sort_order, is_required=s.is_required,
            ))
        await db.flush()
        return await self.get_template_detail(db, template.id, organization_id)

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
        if data.sections is not None:
            for old in list(template.sections):
                await db.delete(old)
            await db.flush()
            for s in data.sections:
                db.add(DailyReportTemplateSection(
                    template_id=template.id, title=s.title, description=s.description,
                    sort_order=s.sort_order, is_required=s.is_required,
                ))
            await db.flush()
        return await self.get_template_detail(db, template.id, organization_id)

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
        await db.delete(template)
        await db.flush()

    # --- Report CRUD ---

    async def get_template(self, db: AsyncSession, organization_id: UUID, store_id: UUID | None = None):
        template = await daily_report_template_repository.get_template_for_store(db, organization_id, store_id)
        if not template:
            raise NotFoundError("사용 가능한 보고서 템플릿이 없습니다")
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
            raise NotFoundError("일일 보고서를 찾을 수 없습니다")
        return report

    async def create_report(
        self, db: AsyncSession, organization_id: UUID, author_id: UUID, data: DailyReportCreate
    ):
        store_id = UUID(data.store_id)
        report_date = date.fromisoformat(data.report_date)

        existing = await daily_report_repository.find_duplicate(db, store_id, report_date, data.period)
        if existing:
            raise HTTPException(status_code=409, detail={
                "message": "해당 매장/날짜/시간대에 이미 보고서가 존재합니다",
                "existing_report_id": str(existing.id),
                "status": existing.status,
            })

        template_id = UUID(data.template_id) if data.template_id else None
        if template_id:
            template = await daily_report_template_repository.get_by_id_with_sections(db, template_id)
        else:
            template = await daily_report_template_repository.get_template_for_store(db, organization_id, store_id)
        if not template:
            raise NotFoundError("사용 가능한 보고서 템플릿이 없습니다")

        # Snapshot template sections to JSONB
        sections_snapshot = [
            {
                "title": ts.title,
                "description": ts.description,
                "content": None,
                "sort_order": ts.sort_order,
                "is_required": ts.is_required,
            }
            for ts in template.sections
        ]

        report = DailyReport(
            organization_id=organization_id,
            store_id=store_id,
            template_id=template.id,
            author_id=author_id,
            report_date=report_date,
            period=data.period,
            status="draft",
            sections_data=sections_snapshot,
        )
        db.add(report)
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

        # Update content in JSONB by sort_order
        sections = list(report.sections_data or [])
        update_map = {u.sort_order: u.content for u in data.sections}
        for section in sections:
            if section["sort_order"] in update_map:
                section["content"] = update_map[section["sort_order"]]

        # Force SQLAlchemy to detect JSONB mutation
        report.sections_data = sections
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

    async def delete_report(
        self, db: AsyncSession, report_id: UUID, organization_id: UUID, author_id: UUID | None = None
    ) -> None:
        report = await daily_report_repository.get_with_details(db, report_id, organization_id)
        if not report:
            raise NotFoundError("일일 보고서를 찾을 수 없습니다")
        # If author_id provided, only author can delete drafts
        if author_id:
            if report.author_id != author_id:
                raise ForbiddenError("본인이 작성한 보고서만 삭제할 수 있습니다")
            if report.status != "draft":
                raise BadRequestError("작성 중인 보고서만 삭제할 수 있습니다")
        await db.delete(report)
        await db.flush()

    async def add_comment(
        self, db: AsyncSession, report_id: UUID, organization_id: UUID, user_id: UUID, data: DailyReportCommentCreate
    ):
        report = await daily_report_repository.get_with_details(db, report_id, organization_id)
        if not report:
            raise NotFoundError("일일 보고서를 찾을 수 없습니다")
        comment = DailyReportComment(report_id=report.id, user_id=user_id, content=data.content)
        db.add(comment)
        await db.flush()
        await db.refresh(comment)
        return comment

    async def build_response(self, db: AsyncSession, report: DailyReport, include_details: bool = False) -> dict:
        user_result = await db.execute(select(User.full_name).where(User.id == report.author_id))
        author_name = user_result.scalar() or "Unknown"
        store_result = await db.execute(select(Store.name).where(Store.id == report.store_id))
        store_name = store_result.scalar()

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
            resp["sections"] = report.sections_data or []
            comments = []
            for c in report.comments:
                cu_result = await db.execute(select(User.full_name).where(User.id == c.user_id))
                cu_name = cu_result.scalar() or "Unknown"
                comments.append({
                    "id": str(c.id), "user_id": str(c.user_id),
                    "user_name": cu_name, "content": c.content,
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
                {"id": str(s.id), "title": s.title, "description": s.description,
                 "sort_order": s.sort_order, "is_required": s.is_required}
                for s in template.sections
            ],
        }


daily_report_service: DailyReportService = DailyReportService()
