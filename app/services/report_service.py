"""Service for unified multi-type Report.

타입별 로직(검증, 본문 생성 등)은 모두 이 service에 모음.
새 타입 추가 시 type 분기를 늘리는 방식. 분기가 많아지면 strategy 패턴으로
type별 클래스 분리 고려.
"""
import uuid
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from sqlalchemy import or_

from app.core.permissions import GM_PRIORITY, SV_PRIORITY
from app.models.organization import Store
from app.models.report import Report, ReportComment, ReportTemplate
from app.models.user import Role, User
from app.models.user_store import UserStore
from app.repositories.report_repository import (
    report_repository,
    report_template_repository,
)
from app.schemas.report import (
    ReportCommentCreate,
    ReportCreate,
    ReportTemplateCreate,
    ReportTemplateUpdate,
    ReportUpdate,
)
from app.utils.exceptions import BadRequestError, ForbiddenError, NotFoundError


# ── 타입별 검증/본문 빌더 ────────────────────────────────────


async def _validate_issue_links(
    db: AsyncSession,
    organization_id: UUID,
    store_id: UUID,
    links: dict[str, Any] | None,
) -> None:
    """payload.links에 들어 있는 ID들이 해당 매장/조직에 속하는지 검증.

    검증 통과 조건:
    - schedule_ids: schedules.store_id == store_id
    - checklist_instance_ids: checklist_instances.store_id == store_id
    - position_ids: positions.store_id == store_id
    - work_role_ids: store_work_roles.store_id == store_id
    - related_user_ids: users.organization_id == organization_id (매장 소속까진 강제 안 함)
    """
    if not links:
        return

    from app.models.schedule import Schedule, StoreWorkRole
    from app.models.checklist import ChecklistInstance
    from app.models.work import Position

    def _parse_uuids(values: Any, field: str) -> list[UUID]:
        if not values:
            return []
        try:
            return [UUID(v) for v in values]
        except (TypeError, ValueError):
            raise BadRequestError(f"links.{field} contains invalid UUID")

    schedule_ids = _parse_uuids(links.get("schedule_ids"), "schedule_ids")
    if schedule_ids:
        rows = await db.execute(
            select(Schedule.id).where(
                Schedule.id.in_(schedule_ids),
                Schedule.store_id == store_id,
            )
        )
        found = {r[0] for r in rows.all()}
        missing = [str(x) for x in schedule_ids if x not in found]
        if missing:
            raise BadRequestError(
                f"links.schedule_ids contain ids not in this store: {missing}"
            )

    cl_ids = _parse_uuids(links.get("checklist_instance_ids"), "checklist_instance_ids")
    if cl_ids:
        rows = await db.execute(
            select(ChecklistInstance.id).where(
                ChecklistInstance.id.in_(cl_ids),
                ChecklistInstance.store_id == store_id,
            )
        )
        found = {r[0] for r in rows.all()}
        missing = [str(x) for x in cl_ids if x not in found]
        if missing:
            raise BadRequestError(
                f"links.checklist_instance_ids contain ids not in this store: {missing}"
            )

    pos_ids = _parse_uuids(links.get("position_ids"), "position_ids")
    if pos_ids:
        rows = await db.execute(
            select(Position.id).where(
                Position.id.in_(pos_ids),
                Position.store_id == store_id,
            )
        )
        found = {r[0] for r in rows.all()}
        missing = [str(x) for x in pos_ids if x not in found]
        if missing:
            raise BadRequestError(
                f"links.position_ids contain ids not in this store: {missing}"
            )

    role_ids = _parse_uuids(links.get("work_role_ids"), "work_role_ids")
    if role_ids:
        rows = await db.execute(
            select(StoreWorkRole.id).where(
                StoreWorkRole.id.in_(role_ids),
                StoreWorkRole.store_id == store_id,
            )
        )
        found = {r[0] for r in rows.all()}
        missing = [str(x) for x in role_ids if x not in found]
        if missing:
            raise BadRequestError(
                f"links.work_role_ids contain ids not in this store: {missing}"
            )

    user_ids = _parse_uuids(links.get("related_user_ids"), "related_user_ids")
    if user_ids:
        rows = await db.execute(
            select(User.id).where(
                User.id.in_(user_ids),
                User.organization_id == organization_id,
            )
        )
        found = {r[0] for r in rows.all()}
        missing = [str(x) for x in user_ids if x not in found]
        if missing:
            raise BadRequestError(
                f"links.related_user_ids contain ids not in this organization: {missing}"
            )

    # role 약어 검증 — staff / sv / gm / owner / all (system role).
    related_roles = links.get("related_roles") or []
    if related_roles:
        valid = {"staff", "sv", "gm", "owner", "all"}
        invalid = [r for r in related_roles if r not in valid]
        if invalid:
            raise BadRequestError(
                f"links.related_roles must be one of {sorted(valid)}; got {invalid}"
            )


def _build_daily_payload_from_template(template: ReportTemplate, period: str) -> dict[str, Any]:
    """daily 리포트 생성 시 템플릿 sections를 본문 sections로 변환."""
    tpl_sections = (template.payload or {}).get("sections", []) or []
    sections = []
    for ts in sorted(tpl_sections, key=lambda s: s.get("sort_order", 0)):
        sections.append({
            "id": str(uuid.uuid4()),
            "title": ts.get("title", ""),
            "content": None,
            "sort_order": ts.get("sort_order", 0),
            "template_section_id": ts.get("id"),
        })
    return {"period": period, "sections": sections}


def _issue_visibility_clause(user: User):
    """이슈 리포트 visibility 추가 조건.

    - SV+ (priority <= SV_PRIORITY): 매장 내 모든 이슈 (accessible_store_ids로 처리, 추가 조건 None)
    - Staff (priority > SV_PRIORITY): 자기 작성 OR extra_viewers.user_ids에 자신
      OR payload.share_with_store_all=True (작성자가 전체 공유 토글)
    """
    priority = user.role.priority if user.role else 999
    if priority <= SV_PRIORITY:
        return None
    user_str = str(user.id)
    return or_(
        Report.author_id == user.id,
        Report.payload["extra_viewers"]["user_ids"].op("?")(user_str),
        Report.payload["share_with_store_all"].astext == "true",
    )


async def _resolve_issue_viewers(
    db: AsyncSession, report: Report
) -> set[UUID]:
    """이슈 리포트의 조회권자 user_id 집합.

    - 작성자
    - 매장 SV+ (role priority <= SV_PRIORITY 이고 user_stores에 해당 매장 있음)
    - payload.extra_viewers.user_ids
    - payload.extra_viewers.position_ids 는 향후 (position-user 매핑 도입 후)
    """
    viewers: set[UUID] = set()
    if report.author_id:
        viewers.add(report.author_id)

    if report.store_id:
        # 매장의 SV+ user
        q = (
            select(User.id)
            .join(Role, Role.id == User.role_id)
            .join(UserStore, UserStore.user_id == User.id)
            .where(
                UserStore.store_id == report.store_id,
                Role.priority <= SV_PRIORITY,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        res = await db.execute(q)
        viewers.update(row[0] for row in res)

    extra = (report.payload or {}).get("extra_viewers", {}) or {}
    for uid in extra.get("user_ids", []) or []:
        try:
            viewers.add(UUID(uid))
        except (ValueError, TypeError):
            continue
    # share_with_store_all=True면 매장 전체 staff 추가
    if (report.payload or {}).get("share_with_store_all") and report.store_id:
        all_q = (
            select(User.id)
            .join(UserStore, UserStore.user_id == User.id)
            .where(
                UserStore.store_id == report.store_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        all_res = await db.execute(all_q)
        viewers.update(row[0] for row in all_res)
    return viewers


async def _resolve_issue_managers(
    db: AsyncSession, report: Report
) -> set[UUID]:
    """이슈 리포트의 매장 관리자(GM+) user_id 집합. 사용자 표현 '관리자는 무조건 받음' 대상.

    매장 SV+에서 더 좁혀 매장 GM+(priority <= GM_PRIORITY) 또는 user_stores.is_manager=True.
    """
    managers: set[UUID] = set()
    if not report.store_id:
        return managers
    q = (
        select(User.id)
        .join(Role, Role.id == User.role_id)
        .join(UserStore, UserStore.user_id == User.id)
        .where(
            UserStore.store_id == report.store_id,
            or_(Role.priority <= GM_PRIORITY, UserStore.is_manager.is_(True)),
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
    )
    res = await db.execute(q)
    managers.update(row[0] for row in res)
    return managers


def _apply_section_updates(report: Report, updates: list) -> None:
    """report.payload.sections의 content를 sort_order 매핑으로 업데이트.

    JSONB는 in-place mutation을 SQLAlchemy가 자동 감지하지 못하므로
    flag_modified 호출 필요.
    """
    if not isinstance(report.payload, dict):
        return
    sections = list(report.payload.get("sections") or [])
    by_sort = {u.sort_order: u.content for u in updates}
    for s in sections:
        so = s.get("sort_order")
        if so in by_sort:
            s["content"] = by_sort[so]
    report.payload = {**report.payload, "sections": sections}
    flag_modified(report, "payload")


class ReportService:

    # ── Template CRUD ──────────────────────────────────────

    async def list_templates(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        type: str | None = None,
        store_id: UUID | None = None,
        is_active: bool | None = None,
    ) -> list[ReportTemplate]:
        return await report_template_repository.list_for_org(
            db, type=type, organization_id=organization_id,
            store_id=store_id, is_active=is_active,
        )

    async def get_template_detail(
        self, db: AsyncSession, template_id: UUID, organization_id: UUID
    ) -> ReportTemplate:
        t = await report_template_repository.get_by_id(db, template_id)
        if not t or (t.organization_id and t.organization_id != organization_id):
            raise NotFoundError("Template not found")
        return t

    async def create_template(
        self, db: AsyncSession, organization_id: UUID, data: ReportTemplateCreate
    ) -> ReportTemplate:
        try:
            t = ReportTemplate(
                type=data.type,
                organization_id=organization_id,
                store_id=UUID(data.store_id) if data.store_id else None,
                name=data.name,
                is_default=data.is_default,
                is_active=True,
                payload=data.payload or {},
            )
            db.add(t)
            await db.flush()
            await db.refresh(t)
            await db.commit()
            return t
        except Exception:
            await db.rollback()
            raise

    async def update_template(
        self,
        db: AsyncSession,
        template_id: UUID,
        organization_id: UUID,
        data: ReportTemplateUpdate,
    ) -> ReportTemplate:
        t = await self.get_template_detail(db, template_id, organization_id)
        try:
            if data.name is not None:
                t.name = data.name
            if data.is_default is not None:
                t.is_default = data.is_default
            if data.is_active is not None:
                t.is_active = data.is_active
            if data.payload is not None:
                t.payload = data.payload
            await db.flush()
            await db.refresh(t)
            await db.commit()
            return t
        except Exception:
            await db.rollback()
            raise

    async def delete_template(
        self, db: AsyncSession, template_id: UUID, organization_id: UUID
    ) -> None:
        t = await self.get_template_detail(db, template_id, organization_id)
        try:
            await db.delete(t)
            await db.flush()
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def get_template_for_use(
        self,
        db: AsyncSession,
        *,
        type: str,
        organization_id: UUID,
        store_id: UUID | None = None,
    ) -> ReportTemplate:
        t = await report_template_repository.get_template_for_store(
            db, type=type, organization_id=organization_id, store_id=store_id,
        )
        if not t:
            raise NotFoundError(f"No available {type} report template")
        return t

    # ── Report CRUD ────────────────────────────────────────

    async def list_reports(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        type: str | None = None,
        store_id: UUID | None = None,
        author_id: UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        period: str | None = None,
        status: str | None = None,
        exclude_draft: bool = True,
        page: int = 1,
        per_page: int = 20,
        accessible_store_ids: list[UUID] | None = None,
        viewer: User | None = None,
        show_all: bool = False,
    ):
        payload_filters: dict | None = None
        if period:
            payload_filters = {"period": period}
        exclude_status = "draft" if (status is None and exclude_draft) else None

        # issue 타입은 staff(priority > SV)에게 visibility 필터 적용.
        # show_all=True면 매장 관리자(SV+)가 일부러 전체 보기 모드로 전환한 경우 — 무시.
        extra_clause = None
        if type == "issue" and viewer is not None and not show_all:
            extra_clause = _issue_visibility_clause(viewer)

        return await report_repository.get_by_org(
            db, organization_id,
            type=type, store_id=store_id, author_id=author_id,
            date_from=date_from, date_to=date_to,
            status=status, exclude_status=exclude_status,
            payload_filters=payload_filters,
            extra_clause=extra_clause,
            page=page, per_page=per_page,
            accessible_store_ids=accessible_store_ids,
        )

    async def get_report(
        self, db: AsyncSession, report_id: UUID, organization_id: UUID
    ) -> Report:
        r = await report_repository.get_with_details(db, report_id, organization_id)
        if not r:
            raise NotFoundError("Report not found")
        return r

    async def create_report(
        self,
        db: AsyncSession,
        organization_id: UUID,
        author_id: UUID,
        data: ReportCreate,
    ) -> Report:
        store_id = UUID(data.store_id)

        # type별 분기
        if data.type == "daily":
            if not data.report_date:
                raise BadRequestError("report_date is required for daily reports")
            report_date = date.fromisoformat(data.report_date)
            period = (data.payload or {}).get("period")
            if period not in ("lunch", "dinner"):
                raise BadRequestError("payload.period must be 'lunch' or 'dinner'")

            # 중복 체크
            existing = await report_repository.find_daily_duplicate(db, store_id, report_date, period)
            if existing:
                raise HTTPException(status_code=409, detail={
                    "message": "A report already exists for this store/date/period",
                    "existing_report_id": str(existing.id),
                    "status": existing.status,
                })

            # 템플릿 결정
            template_id = UUID(data.template_id) if data.template_id else None
            if template_id:
                template = await report_template_repository.get_by_id(db, template_id)
                if not template or template.type != "daily":
                    raise NotFoundError("Template not found")
            else:
                template = await report_template_repository.get_template_for_store(
                    db, type="daily", organization_id=organization_id, store_id=store_id,
                )
            if not template:
                raise NotFoundError("No available daily report template")

            payload = _build_daily_payload_from_template(template, period)
            title = None
        elif data.type == "issue":
            # 이슈 리포트: store template에서 카테고리/커스텀 필드 동적 로딩.
            from app.schemas.report import (
                DEFAULT_ISSUE_CATEGORIES,
                ISSUE_SEVERITIES,
            )
            from app.services.storage_service import storage_service
            raw_payload = dict(data.payload or {})
            category = raw_payload.get("category")
            severity = raw_payload.get("severity")
            if severity not in ISSUE_SEVERITIES:
                raise BadRequestError(
                    f"payload.severity must be one of {ISSUE_SEVERITIES}"
                )
            if not data.title:
                raise BadRequestError("title is required for issue reports")

            # 매장 issue template lookup (store → org → system default)
            template = await report_template_repository.get_template_for_store(
                db, type="issue", organization_id=organization_id, store_id=store_id,
            )
            if template:
                tpl = template.payload or {}
                tpl_categories = tpl.get("categories") or []
                allowed_codes = {
                    c.get("code") for c in tpl_categories if c.get("is_active", True)
                }
                # 카테고리 정의가 비어있으면 시스템 기본 6개로 fallback
                if not allowed_codes:
                    allowed_codes = set(DEFAULT_ISSUE_CATEGORIES)
                custom_fields = tpl.get("custom_fields") or []
            else:
                allowed_codes = set(DEFAULT_ISSUE_CATEGORIES)
                custom_fields = []

            if category not in allowed_codes:
                raise BadRequestError(
                    f"payload.category must be one of {sorted(allowed_codes)}"
                )

            # custom_field_values 검증 (required 체크 + select 옵션 매칭)
            cfv = raw_payload.get("custom_field_values") or {}
            if not isinstance(cfv, dict):
                raise BadRequestError("payload.custom_field_values must be an object")
            for cf in custom_fields:
                cf_id = cf.get("id")
                if not cf_id:
                    continue
                val = cfv.get(cf_id)
                if cf.get("required") and (val is None or val == "" or val == []):
                    raise BadRequestError(f"Custom field '{cf.get('label', cf_id)}' is required")
                ftype = cf.get("type")
                if val in (None, "", []):
                    continue
                if ftype == "number":
                    try:
                        float(val)
                    except (TypeError, ValueError):
                        raise BadRequestError(f"Custom field '{cf_id}' must be a number")
                elif ftype == "single_choice":
                    opts = cf.get("options") or []
                    if val not in opts:
                        raise BadRequestError(
                            f"Custom field '{cf_id}' must be one of {opts}"
                        )
                elif ftype == "multi_choice":
                    opts = cf.get("options") or []
                    if not isinstance(val, list) or any(v not in opts for v in val):
                        raise BadRequestError(
                            f"Custom field '{cf_id}' values must all be in {opts}"
                        )

            # attachments key 정규화 (temp → 최종). 멱등.
            attachments = raw_payload.get("attachments") or []
            finalized: list[dict] = []
            for a in attachments:
                if not isinstance(a, dict):
                    continue
                key_or_url = a.get("key") or a.get("url")
                if not key_or_url:
                    continue
                try:
                    final_key = storage_service.finalize_upload(key_or_url)
                except Exception:
                    final_key = key_or_url
                finalized.append({**a, "key": final_key})
            raw_payload["attachments"] = finalized

            # links 검증: 모든 ID들이 매장/조직에 속해야 함
            await _validate_issue_links(
                db, organization_id, store_id, raw_payload.get("links")
            )

            # issue 는 report_date 가 명시 안 됐으면 today 로 자동 set
            # (date range 필터에서 매칭되도록).
            report_date = (
                date.fromisoformat(data.report_date)
                if data.report_date
                else date.today()
            )
            payload = raw_payload
            title = data.title
        else:
            template = None
            if data.template_id:
                template = await report_template_repository.get_by_id(db, UUID(data.template_id))
            report_date = date.fromisoformat(data.report_date) if data.report_date else None
            payload = data.payload or {}
            title = data.title

        # 타입별 초기 status
        initial_status = "open" if data.type == "issue" else "draft"

        try:
            r = Report(
                type=data.type,
                organization_id=organization_id,
                store_id=store_id,
                template_id=template.id if template else None,
                author_id=author_id,
                title=title,
                status=initial_status,
                report_date=report_date,
                payload=payload,
            )
            db.add(r)
            await db.flush()
            await db.refresh(r)
            await db.commit()
            # 이슈는 생성 즉시 조회권자 전원에게 알림
            if r.type == "issue":
                await self._notify_issue_event(db, report=r, event="created", actor_id=author_id)
            return r
        except Exception:
            await db.rollback()
            raise

    async def update_report(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
        author_id: UUID,
        data: ReportUpdate,
        is_manager: bool = False,
    ) -> Report:
        """리포트 본문 수정.

        - daily: 작성자만, draft 상태에서만.
        - issue: 작성자 OR 매니저(GM+) 가능, closed 상태는 거부.
        """
        r = await self.get_report(db, report_id, organization_id)
        if r.type == "daily":
            if r.author_id != author_id:
                raise ForbiddenError("Only the author can update this report")
            if r.status != "draft":
                raise BadRequestError("Only draft daily reports can be updated")
        elif r.type == "issue":
            if r.author_id != author_id and not is_manager:
                raise ForbiddenError("Only the author or a manager can update this report")
            if r.status == "closed":
                raise BadRequestError("Closed issue reports cannot be updated")
        else:
            if r.author_id != author_id:
                raise ForbiddenError("Only the author can update this report")
        try:
            if data.title is not None:
                r.title = data.title
            if data.payload is not None:
                # issue 타입은 links 검증
                if r.type == "issue":
                    await _validate_issue_links(
                        db,
                        organization_id,
                        r.store_id,
                        data.payload.get("links") if isinstance(data.payload, dict) else None,
                    )
                r.payload = data.payload
                flag_modified(r, "payload")
            if data.sections is not None:
                _apply_section_updates(r, data.sections)
            await db.flush()
            await db.refresh(r)
            await db.commit()
            return r
        except Exception:
            await db.rollback()
            raise

    async def transition_issue_status(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
        actor_id: UUID,
        new_status: str,
    ) -> Report:
        """이슈 상태 전이 (open → in_progress → closed). 관리자(SV+) 권한."""
        from app.schemas.report import ISSUE_STATUSES
        if new_status not in ISSUE_STATUSES:
            raise BadRequestError(f"Invalid status. Allowed: {ISSUE_STATUSES}")
        r = await self.get_report(db, report_id, organization_id)
        if r.type != "issue":
            raise BadRequestError("Only issue reports support status transition")
        if r.status == new_status:
            return r
        try:
            r.status = new_status
            await db.flush()
            await db.refresh(r)
            await db.commit()
            await self._notify_issue_event(
                db, report=r, event=f"status:{new_status}", actor_id=actor_id
            )
            return r
        except Exception:
            await db.rollback()
            raise

    async def submit_report(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
        author_id: UUID,
    ) -> Report:
        r = await self.get_report(db, report_id, organization_id)
        if r.author_id != author_id:
            raise ForbiddenError("Only the author can submit this report")
        if r.status != "draft":
            raise BadRequestError("Only draft reports can be submitted")
        try:
            r.status = "submitted"
            r.submitted_at = datetime.now(timezone.utc)
            await db.flush()
            await db.refresh(r)
            await db.commit()
            return r
        except Exception:
            await db.rollback()
            raise

    async def delete_report(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
        author_id: UUID | None = None,
    ) -> None:
        r = await report_repository.get_with_details(db, report_id, organization_id)
        if not r:
            raise NotFoundError("Report not found")
        if author_id:
            if r.author_id != author_id:
                raise ForbiddenError("Only the author can delete this report")
            if r.status != "draft":
                raise BadRequestError("Only draft reports can be deleted")
        try:
            # soft delete (drafts에서도 동일 처리. 필요시 hard delete로 변경)
            r.deleted_at = datetime.now(timezone.utc)
            await db.flush()
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def add_comment(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
        user_id: UUID,
        data: ReportCommentCreate,
    ) -> ReportComment:
        r = await report_repository.get_with_details(db, report_id, organization_id)
        if not r:
            raise NotFoundError("Report not found")
        try:
            c = ReportComment(report_id=r.id, user_id=user_id, content=data.content)
            db.add(c)
            await db.flush()
            await db.refresh(c)
            await db.commit()
            if r.type == "issue":
                await self._notify_issue_event(
                    db, report=r, event="comment", actor_id=user_id, excerpt=data.content
                )
            else:
                await self._notify_reply(db, report=r, author_id=user_id, excerpt=data.content)
            return c
        except Exception:
            await db.rollback()
            raise

    async def _notify_issue_event(
        self,
        db: AsyncSession,
        *,
        report: Report,
        event: str,
        actor_id: UUID,
        excerpt: str | None = None,
    ) -> None:
        """이슈 리포트 이벤트 알림.

        event: "created" | "status:open" | "status:in_progress" | "status:closed" | "comment"
        - 조회권자 전원에게 in-app alert
        - 매장 관리자(GM+)는 무조건 이메일도 (alert preference 무시 가능 옵션)
        - 본인이 친 액션은 본인 제외
        """
        try:
            from app.services.alert_service import alert_service
            from app.utils.email import send_email
            from app.utils.email_templates import build_reply_email
            import asyncio

            viewers = await _resolve_issue_viewers(db, report)
            managers = await _resolve_issue_managers(db, report)
            # 본인 제외
            viewers.discard(actor_id)
            managers.discard(actor_id)

            # 액션 actor 이름
            actor_r = await db.execute(select(User.full_name).where(User.id == actor_id))
            actor_name = actor_r.scalar() or "Someone"

            severity = (report.payload or {}).get("severity", "")
            category = (report.payload or {}).get("category", "")
            subtitle_parts = []
            if category:
                subtitle_parts.append(category)
            if severity:
                subtitle_parts.append(severity)
            subtitle = " · ".join(subtitle_parts) or "issue"

            if event == "created":
                context_label = "issue report"
                excerpt_text = report.title or excerpt
            elif event.startswith("status:"):
                new_status = event.split(":", 1)[1]
                context_label = f"issue {new_status}"
                excerpt_text = report.title
            else:
                context_label = "issue report"
                excerpt_text = excerpt

            # 1) viewers 전원에게 in-app alert
            for uid in viewers:
                try:
                    await alert_service.create_for_reply(
                        db,
                        organization_id=report.organization_id,
                        recipient_id=uid,
                        author_name=actor_name,
                        context_label=context_label,
                        reference_type="issue_report",
                        reference_id=report.id,
                    )
                except Exception:
                    pass
            await db.commit()

            # 2) 매장 관리자(GM+)에게 이메일 (alert pref 무시 = 무조건)
            for uid in managers:
                recipient = await db.execute(
                    select(User.full_name, User.email).where(User.id == uid)
                )
                row = recipient.first()
                if not row or not row.email:
                    continue
                subject, html = build_reply_email(
                    recipient_name=row.full_name or "there",
                    author_name=actor_name,
                    context_label=context_label.title(),
                    context_subtitle=f"{subtitle} · {report.title or ''}".strip(" ·"),
                    excerpt=(excerpt_text[:160] if excerpt_text else None),
                    cta_url=None,
                )
                asyncio.create_task(send_email(to=row.email, subject=subject, html=html))
        except Exception:
            pass

    async def _notify_reply(
        self,
        db: AsyncSession,
        *,
        report: Report,
        author_id: UUID,
        excerpt: str | None,
    ) -> None:
        """리포트에 코멘트가 달렸을 때 작성자에게 알림 + 이메일."""
        recipient_id: UUID | None = report.author_id
        if recipient_id is None or recipient_id == author_id:
            return
        try:
            from app.services.alert_service import alert_service
            from app.utils.email import send_email
            from app.utils.email_templates import build_reply_email
            import asyncio

            author_r = await db.execute(select(User.full_name).where(User.id == author_id))
            author_name = author_r.scalar() or "Manager"
            recipient_r = await db.execute(
                select(User.full_name, User.email).where(User.id == recipient_id)
            )
            row = recipient_r.first()
            recipient_name = (row.full_name if row else None) or "there"
            recipient_email = row.email if row else None

            # context label/subtitle: type별
            context_label = "report"
            subtitle = ""
            if report.type == "daily":
                period = (report.payload or {}).get("period", "")
                period_label = "Lunch" if period == "lunch" else "Dinner" if period == "dinner" else str(period)
                subtitle = f"{report.report_date} · {period_label}"
                context_label = "daily report"
            else:
                if report.title:
                    subtitle = report.title
                elif report.report_date:
                    subtitle = str(report.report_date)
                context_label = f"{report.type} report"

            await alert_service.create_for_reply(
                db,
                organization_id=report.organization_id,
                recipient_id=recipient_id,
                author_name=author_name,
                context_label=context_label,
                reference_type=f"{report.type}_report",
                reference_id=report.id,
            )
            await db.commit()

            if recipient_email and await alert_service.should_send_email(
                db, recipient_id, "reply"
            ):
                subject, html = build_reply_email(
                    recipient_name=recipient_name,
                    author_name=author_name,
                    context_label=context_label.title(),
                    context_subtitle=subtitle,
                    excerpt=(excerpt[:160] if excerpt else None),
                    cta_url=None,
                )
                asyncio.create_task(send_email(to=recipient_email, subject=subject, html=html))
        except Exception:
            pass

    # ── Response builders ──────────────────────────────────

    def _resolve_payload_attachments(self, payload: dict) -> dict:
        """payload.attachments[].key → url 추가."""
        if not isinstance(payload, dict):
            return payload
        attachments = payload.get("attachments")
        if not attachments:
            return payload
        from app.services.storage_service import storage_service
        resolved = []
        for a in attachments:
            item = dict(a) if isinstance(a, dict) else {}
            key = item.get("key")
            if key:
                item["url"] = storage_service.resolve_url(key)
            resolved.append(item)
        return {**payload, "attachments": resolved}

    def _to_dict(
        self,
        r: Report,
        author_name: str | None,
        store_name: str | None,
        include_comments: bool = False,
        comment_user_names: dict | None = None,
    ) -> dict:
        try:
            comment_count = len(r.comments)
        except Exception:
            comment_count = 0
        resp = {
            "id": str(r.id),
            "type": r.type,
            "organization_id": str(r.organization_id),
            "store_id": str(r.store_id) if r.store_id else None,
            "store_name": store_name,
            "template_id": str(r.template_id) if r.template_id else None,
            "author_id": str(r.author_id) if r.author_id else None,
            "author_name": author_name,
            "title": r.title,
            "status": r.status,
            "report_date": r.report_date,
            "submitted_at": r.submitted_at,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
            "payload": self._resolve_payload_attachments(r.payload or {}),
            "comment_count": comment_count,
        }
        if include_comments:
            names = comment_user_names or {}
            resp["comments"] = [
                {
                    "id": str(c.id),
                    "user_id": str(c.user_id) if c.user_id else None,
                    "user_name": names.get(c.user_id) or "Unknown",
                    "content": c.content,
                    "created_at": c.created_at,
                }
                for c in r.comments
            ]
        else:
            resp["comments"] = []
        return resp

    async def build_response(
        self, db: AsyncSession, report: Report, include_comments: bool = True
    ) -> dict:
        author_name: str | None = None
        if report.author_id:
            u = await db.execute(select(User.full_name).where(User.id == report.author_id))
            author_name = u.scalar()
        store_name: str | None = None
        if report.store_id:
            s = await db.execute(select(Store.name).where(Store.id == report.store_id))
            store_name = s.scalar()

        comment_user_names = None
        if include_comments:
            try:
                ids = list({c.user_id for c in report.comments if c.user_id})
            except Exception:
                ids = []
            if ids:
                cu = await db.execute(
                    select(User.id, User.full_name).where(User.id.in_(ids))
                )
                comment_user_names = {row.id: row.full_name for row in cu}
        return self._to_dict(report, author_name, store_name, include_comments, comment_user_names)

    async def build_responses_batch(
        self, db: AsyncSession, reports: list[Report]
    ) -> list[dict]:
        author_ids = list({r.author_id for r in reports if r.author_id})
        store_ids = list({r.store_id for r in reports if r.store_id})
        author_names: dict = {}
        if author_ids:
            res = await db.execute(select(User.id, User.full_name).where(User.id.in_(author_ids)))
            author_names = {row.id: row.full_name for row in res}
        store_names: dict = {}
        if store_ids:
            res = await db.execute(select(Store.id, Store.name).where(Store.id.in_(store_ids)))
            store_names = {row.id: row.name for row in res}
        return [
            self._to_dict(
                r,
                author_names.get(r.author_id) if r.author_id else None,
                store_names.get(r.store_id) if r.store_id else None,
            )
            for r in reports
        ]

    def build_template_response(self, template: ReportTemplate) -> dict:
        return {
            "id": str(template.id),
            "type": template.type,
            "organization_id": str(template.organization_id) if template.organization_id else None,
            "store_id": str(template.store_id) if template.store_id else None,
            "name": template.name,
            "is_default": template.is_default,
            "is_active": template.is_active,
            "payload": template.payload or {},
            "created_at": template.created_at,
        }


report_service: ReportService = ReportService()
