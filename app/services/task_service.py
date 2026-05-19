"""Task service — issue_report 에서 promote 또는 직접 생성되는 work item.

명칭 변경 이력: additional_tasks → issues → tasks. report 의 payload key 인
linked_task_id 가 issue 시절엔 linked_issue_id 였음 (호환 위해 양쪽 모두 인식).
"""
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.task import Task, TaskAssignee, TaskComment
from app.models.organization import Store
from app.models.report import Report
from app.models.user import User
from app.schemas.task import TaskCreate, TaskPromoteRequest, TaskUpdate
from app.services.storage_service import storage_service
from app.utils.exceptions import BadRequestError, NotFoundError, ForbiddenError

# report.payload 의 linked task id 키 — 신/구 모두 인식 (구버전 데이터 호환).
LINKED_TASK_KEYS = ("linked_task_id", "linked_issue_id")


def _get_linked_task_id(payload: dict | None) -> str | None:
    """report.payload 에서 linked_task_id 추출. 구버전 키 (linked_issue_id) 도 인식."""
    if not isinstance(payload, dict):
        return None
    for k in LINKED_TASK_KEYS:
        v = payload.get(k)
        if v:
            return str(v)
    return None


def _clear_linked_task_keys(payload: dict) -> None:
    """payload 에서 linked_task_id / linked_issue_id 모두 제거 (in-place)."""
    for k in LINKED_TASK_KEYS:
        payload.pop(k, None)


def _normalize_attachments(items: list | None) -> list[dict]:
    """업로드된 file_url 을 storage key 로 정리 + url 필드 제거 (DB 저장용)."""
    if not items:
        return []
    out: list[dict] = []
    for a in items:
        d = a.model_dump() if hasattr(a, "model_dump") else dict(a)
        # client 가 url 또는 key 로 보낼 수 있음 → finalize_upload 로 key 추출.
        key_or_url = d.get("key") or d.get("url")
        if key_or_url:
            d["key"] = storage_service.finalize_upload(key_or_url)
        d.pop("url", None)  # DB 에는 url 저장 X
        out.append(d)
    return out


# status 전이 규칙 — (from, to): require_manager
_STATUS_TRANSITIONS: dict[tuple[str, str], bool] = {
    ("pending", "in_progress"): False,         # assignee 가 시작
    ("in_progress", "under_review"): False,    # assignee 가 제출
    ("under_review", "completed"): True,       # manager 가 승인
    ("under_review", "in_progress"): True,     # manager 가 반려
    ("completed", "in_progress"): True,        # manager 가 reopen
    ("in_progress", "pending"): True,          # manager 가 되돌림
}


class TaskService:
    async def list_tasks(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        store_id: UUID | None = None,
        status: str | None = None,
        category: str | None = None,
        assignee_id: UUID | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[Task], int]:
        from sqlalchemy import func
        q = (
            select(Task)
            .where(Task.organization_id == organization_id, Task.deleted_at.is_(None))
        )
        if store_id:
            # 1) legacy store_id 매칭 OR 2) store_ids JSONB array 에 포함 OR
            # 3) org-wide task (store_ids = [], legacy store_id IS NULL).
            from sqlalchemy import or_, cast
            from sqlalchemy.dialects.postgresql import JSONB
            sid_str = str(store_id)
            q = q.where(
                or_(
                    Task.store_id == store_id,
                    Task.store_ids.contains(cast([sid_str], JSONB)),
                    # org-wide: 모든 store 대상 → store filter 와도 매칭
                    Task.store_ids == cast([], JSONB),
                )
            )
        if status:
            q = q.where(Task.status == status)
        if category:
            q = q.where(Task.category == category)
        if assignee_id:
            q = q.join(TaskAssignee, TaskAssignee.task_id == Task.id).where(
                TaskAssignee.user_id == assignee_id
            )
        count = await db.execute(select(func.count()).select_from(q.subquery()))
        total = count.scalar() or 0
        q = (
            q.options(selectinload(Task.assignees))
            .order_by(Task.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        result = await db.execute(q)
        return list(result.scalars().all()), total

    async def get_task(
        self, db: AsyncSession, task_id: UUID, organization_id: UUID
    ) -> Task:
        q = (
            select(Task)
            .options(selectinload(Task.assignees))
            .where(
                Task.id == task_id,
                Task.organization_id == organization_id,
                Task.deleted_at.is_(None),
            )
        )
        result = await db.execute(q)
        task = result.scalar_one_or_none()
        if not task:
            raise NotFoundError("Task not found")
        return task

    async def create_task(
        self,
        db: AsyncSession,
        organization_id: UUID,
        creator_id: UUID,
        data: TaskCreate,
    ) -> Task:
        try:
            # 데이터 schema 는 issue report 와 호환되므로 prefill 흐름이 자연스러움.
            # 클라이언트가 task UI 에서 명시한 값을 우선 사용하되, 안 보낸 경우엔
            # source report 의 값으로 자동 채움 (편의).
            links_dict = data.links.model_dump() if data.links else {}
            source_report = None
            source_report_id = UUID(data.source_report_id) if data.source_report_id else None
            inherited_severity = data.severity
            inherited_category = data.category
            if source_report_id:
                rsel = await db.execute(
                    select(Report).where(
                        Report.id == source_report_id,
                        Report.organization_id == organization_id,
                    )
                )
                source_report = rsel.scalar_one_or_none()
                if source_report is None:
                    raise NotFoundError("Source report not found")
                source_payload = source_report.payload or {}
                # 클라이언트가 links 를 안 보낸 경우만 source 의 links 로 채움.
                if not links_dict:
                    src_links = source_payload.get("links") or {}
                    links_dict = {
                        "schedule_ids": list(src_links.get("schedule_ids") or []),
                        "checklist_instance_ids": list(src_links.get("checklist_instance_ids") or []),
                        "position_ids": list(src_links.get("position_ids") or []),
                        "work_role_ids": list(src_links.get("work_role_ids") or []),
                        "related_user_ids": list(src_links.get("related_user_ids") or []),
                        "related_roles": list(src_links.get("related_roles") or []),
                    }
                if inherited_severity is None:
                    inherited_severity = source_payload.get("severity")
                if inherited_category is None:
                    inherited_category = source_payload.get("category")

            # store_ids 가 비어있고 store_id (legacy) 가 있으면 호환 처리.
            store_ids: list[str] = list(data.store_ids or [])
            if not store_ids and data.store_id:
                store_ids = [data.store_id]
            # legacy store_id 컬럼은 store_ids[0] 의 mirror.
            primary_store_id = UUID(store_ids[0]) if store_ids else None

            task = Task(
                organization_id=organization_id,
                store_id=primary_store_id,
                store_ids=store_ids,
                title=data.title,
                description=data.description,
                priority=data.priority or "normal",
                severity=inherited_severity,
                category=inherited_category,
                status="pending",
                due_date=data.due_date,
                created_by=creator_id,
                source_report_id=source_report_id,
                links=links_dict,
                attachments=_normalize_attachments(data.attachments),
            )
            db.add(task)
            await db.flush()
            for uid in data.assignee_ids:
                db.add(TaskAssignee(task_id=task.id, user_id=UUID(uid)))
            # source report 가 지정된 경우 promote 와 동일하게 역참조 + 상태 동기화
            if source_report is not None:
                from sqlalchemy.orm.attributes import flag_modified
                new_payload = dict(source_report.payload or {})
                if not _get_linked_task_id(new_payload):
                    _clear_linked_task_keys(new_payload)
                    new_payload["linked_task_id"] = str(task.id)
                    source_report.payload = new_payload
                    flag_modified(source_report, "payload")
                    if source_report.status == "open":
                        source_report.status = "in_progress"
            await db.flush()
            await db.refresh(task)
            await db.commit()
            return await self.get_task(db, task.id, organization_id)
        except Exception:
            await db.rollback()
            raise

    async def update_task(
        self,
        db: AsyncSession,
        task_id: UUID,
        organization_id: UUID,
        data: TaskUpdate,
    ) -> Task:
        task = await self.get_task(db, task_id, organization_id)
        if data.store_ids is not None:
            from sqlalchemy.orm.attributes import flag_modified
            task.store_ids = list(data.store_ids)
            flag_modified(task, "store_ids")
            task.store_id = UUID(data.store_ids[0]) if data.store_ids else None
        prev_status = task.status
        try:
            if data.title is not None:
                task.title = data.title
            if data.description is not None:
                task.description = data.description
            if data.priority is not None:
                task.priority = data.priority
            if data.severity is not None:
                task.severity = data.severity
            if data.category is not None:
                task.category = data.category
            if data.status is not None:
                task.status = data.status
            if data.due_date is not None:
                task.due_date = data.due_date
            if data.assignee_ids is not None:
                for a in list(task.assignees):
                    await db.delete(a)
                await db.flush()
                for uid in data.assignee_ids:
                    db.add(TaskAssignee(task_id=task.id, user_id=UUID(uid)))
            if data.links is not None:
                from sqlalchemy.orm.attributes import flag_modified
                task.links = data.links.model_dump()
                flag_modified(task, "links")
            if data.attachments is not None:
                from sqlalchemy.orm.attributes import flag_modified
                task.attachments = _normalize_attachments(data.attachments)
                flag_modified(task, "attachments")

            # task completed → source report 자동 closed (sync)
            if (
                task.status == "completed"
                and prev_status != "completed"
                and task.source_report_id
            ):
                from sqlalchemy.orm.attributes import flag_modified
                rsel = await db.execute(
                    select(Report).where(Report.id == task.source_report_id)
                )
                source_report = rsel.scalar_one_or_none()
                if source_report and source_report.status != "closed":
                    source_report.status = "closed"
                    flag_modified(source_report, "status")

            await db.flush()
            await db.refresh(task)
            await db.commit()
            return await self.get_task(db, task.id, organization_id)
        except Exception:
            await db.rollback()
            raise

    async def delete_task(
        self, db: AsyncSession, task_id: UUID, organization_id: UUID
    ) -> None:
        from sqlalchemy.orm.attributes import flag_modified

        task = await self.get_task(db, task_id, organization_id)
        try:
            task.deleted_at = datetime.now(timezone.utc)
            # source report 의 linked_task_id 해제. 재 promote 가능하게.
            if task.source_report_id:
                r = await db.execute(
                    select(Report).where(Report.id == task.source_report_id)
                )
                report = r.scalar_one_or_none()
                if report and isinstance(report.payload, dict):
                    if _get_linked_task_id(report.payload) == str(task.id):
                        new_payload = dict(report.payload)
                        _clear_linked_task_keys(new_payload)
                        report.payload = new_payload
                        flag_modified(report, "payload")
            await db.flush()
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def promote_from_report(
        self,
        db: AsyncSession,
        report_id: UUID,
        organization_id: UUID,
        creator_id: UUID,
        data: TaskPromoteRequest,
    ) -> Task:
        """Issue report → Task 전환.

        - report.payload.linked_task_id 에 새 task id 저장 (역참조)
        - tasks.source_report_id = report.id
        - 같은 report 에서 이미 promote 된 게 있으면 그것을 반환 (멱등)
        """
        from sqlalchemy.orm.attributes import flag_modified

        r = await db.execute(
            select(Report).where(
                Report.id == report_id,
                Report.organization_id == organization_id,
                Report.deleted_at.is_(None),
            )
        )
        report = r.scalar_one_or_none()
        if not report:
            raise NotFoundError("Report not found")
        if report.type != "issue":
            raise BadRequestError("Only issue reports can be promoted")

        # 이미 promote 된 경우
        existing_link = _get_linked_task_id(report.payload)
        if existing_link:
            res = await db.execute(
                select(Task)
                .options(selectinload(Task.assignees))
                .where(Task.id == UUID(existing_link), Task.deleted_at.is_(None))
            )
            existing = res.scalar_one_or_none()
            if existing:
                return existing

        title = data.title or report.title or "Task"
        description = data.description
        if not description:
            description = (report.payload or {}).get("description")

        # Issue report → Task promote 시 데이터 inherit. task UI 가 issue report UI 와
        # 별개라도 데이터 schema 는 호환되므로 자연스럽게 채움. 사용자가 task 단계에서
        # 수정 가능.
        source_links = (report.payload or {}).get("links") or {}
        inherited_links: dict = {
            "schedule_ids": list(source_links.get("schedule_ids") or []),
            "checklist_instance_ids": list(source_links.get("checklist_instance_ids") or []),
            "position_ids": list(source_links.get("position_ids") or []),
            "work_role_ids": list(source_links.get("work_role_ids") or []),
            "related_user_ids": list(source_links.get("related_user_ids") or []),
            "related_roles": list(source_links.get("related_roles") or []),
        }
        inherited_severity = data.severity or (report.payload or {}).get("severity")
        inherited_category = data.category or (report.payload or {}).get("category")

        try:
            # promote 시 source report 의 단일 store 를 그대로 계승.
            promoted_store_ids = [str(report.store_id)] if report.store_id else []
            task = Task(
                organization_id=organization_id,
                store_id=report.store_id,
                store_ids=promoted_store_ids,
                title=title,
                description=description,
                priority=data.priority or "normal",
                severity=inherited_severity,
                category=inherited_category,
                status="pending",
                due_date=data.due_date,
                created_by=creator_id,
                source_report_id=report.id,
                links=inherited_links,
            )
            db.add(task)
            await db.flush()
            for uid in data.assignee_ids:
                db.add(TaskAssignee(task_id=task.id, user_id=UUID(uid)))
            new_payload = dict(report.payload or {})
            _clear_linked_task_keys(new_payload)
            new_payload["linked_task_id"] = str(task.id)
            report.payload = new_payload
            flag_modified(report, "payload")
            if report.status == "open":
                report.status = "in_progress"
            await db.flush()
            await db.refresh(task)
            await db.commit()
            return await self.get_task(db, task.id, organization_id)
        except Exception:
            await db.rollback()
            raise

    # ── status 전이 (under_review / completed / reopen) ──────────────────
    async def transition(
        self,
        db: AsyncSession,
        task_id: UUID,
        organization_id: UUID,
        user: User,
        *,
        next_status: str,
        comment: str | None,
        is_manager: bool,
        attachments: list | None = None,
    ) -> Task:
        task = await self.get_task(db, task_id, organization_id)
        prev = task.status
        key = (prev, next_status)
        if key not in _STATUS_TRANSITIONS:
            raise BadRequestError(
                f"Invalid transition: {prev} → {next_status}"
            )
        require_manager = _STATUS_TRANSITIONS[key]
        if require_manager and not is_manager:
            raise ForbiddenError("Manager permission required for this transition")
        # assignee-only transitions: pending→in_progress, in_progress→under_review
        if not require_manager:
            assignee_ids = {a.user_id for a in task.assignees if a.user_id}
            if user.id not in assignee_ids and not is_manager:
                raise ForbiddenError("Only assignees can perform this transition")

        try:
            now = datetime.now(timezone.utc)
            task.status = next_status
            if next_status == "under_review":
                task.submitted_at = now
                task.submitted_by = user.id
            elif next_status == "completed":
                task.reviewed_at = now
                task.reviewed_by = user.id
            elif next_status == "in_progress" and prev in ("under_review", "completed"):
                # 반려 or reopen — reviewed_at 마킹
                task.reviewed_at = now
                task.reviewed_by = user.id

            # system comment (status 변경 audit) + optional user comment
            sys_label = {
                "pending": "Reset to pending",
                "in_progress": "Reopened" if prev in ("under_review", "completed") else "Started",
                "under_review": "Submitted — under review",
                "completed": "Approved",
            }.get(next_status, f"Status: {next_status}")
            db.add(TaskComment(
                task_id=task.id,
                user_id=user.id,
                content=sys_label,
                kind="system",
            ))
            normalized_attachments = _normalize_attachments(attachments)
            comment_text = (comment or "").strip()
            # 코멘트 텍스트 또는 첨부 중 하나라도 있으면 user comment 생성.
            if comment_text or normalized_attachments:
                db.add(TaskComment(
                    task_id=task.id,
                    user_id=user.id,
                    content=comment_text,
                    kind="comment",
                    attachments=normalized_attachments,
                ))

            # task completed → source report 자동 closed
            if next_status == "completed" and task.source_report_id:
                from sqlalchemy.orm.attributes import flag_modified
                r = await db.execute(
                    select(Report).where(Report.id == task.source_report_id)
                )
                src = r.scalar_one_or_none()
                if src and src.status != "closed":
                    src.status = "closed"
                    flag_modified(src, "status")

            await db.flush()
            await db.commit()
            return await self.get_task(db, task.id, organization_id)
        except Exception:
            await db.rollback()
            raise

    # ── Comments ─────────────────────────────────────────────────────────
    def _serialize_comment(self, c: TaskComment, user_name: str | None) -> dict:
        out_attachments: list[dict] = []
        for a in c.attachments or []:
            d = dict(a) if isinstance(a, dict) else a
            out_attachments.append({
                "key": d.get("key"),
                "url": storage_service.resolve_url(d.get("key")),
                "mime_type": d.get("mime_type"),
                "kind": d.get("kind"),
                "name": d.get("name"),
                "size": d.get("size"),
            })
        return {
            "id": str(c.id),
            "task_id": str(c.task_id),
            "user_id": str(c.user_id) if c.user_id else None,
            "user_name": user_name,
            "content": c.content,
            "kind": c.kind,
            "attachments": out_attachments,
            "created_at": c.created_at,
        }

    async def list_comments(
        self, db: AsyncSession, task_id: UUID, organization_id: UUID
    ) -> list[dict]:
        # 권한·존재 체크
        await self.get_task(db, task_id, organization_id)
        res = await db.execute(
            select(TaskComment)
            .where(TaskComment.task_id == task_id)
            .order_by(TaskComment.created_at)
        )
        comments = list(res.scalars().all())
        user_ids = {c.user_id for c in comments if c.user_id}
        name_map: dict = {}
        if user_ids:
            u_res = await db.execute(
                select(User.id, User.full_name).where(User.id.in_(user_ids))
            )
            name_map = {row.id: row.full_name for row in u_res}
        return [
            self._serialize_comment(c, name_map.get(c.user_id) if c.user_id else None)
            for c in comments
        ]

    async def add_comment(
        self,
        db: AsyncSession,
        task_id: UUID,
        organization_id: UUID,
        user_id: UUID,
        content: str,
        attachments: list | None = None,
    ) -> dict:
        task = await self.get_task(db, task_id, organization_id)
        text = (content or "").strip()
        normalized = _normalize_attachments(attachments)
        if not text and not normalized:
            raise BadRequestError("Comment must have content or attachments")
        try:
            c = TaskComment(
                task_id=task.id,
                user_id=user_id,
                content=text,
                kind="comment",
                attachments=normalized,
            )
            db.add(c)
            await db.flush()
            await db.commit()
            u_res = await db.execute(select(User.full_name).where(User.id == user_id))
            uname = u_res.scalar()
            return self._serialize_comment(c, uname)
        except Exception:
            await db.rollback()
            raise

    async def build_response(self, db: AsyncSession, task: Task) -> dict:
        # store_ids 우선 (legacy store_id 는 store_ids[0] 와 동일).
        raw_store_ids: list[str] = list(task.store_ids or [])
        if not raw_store_ids and task.store_id:
            raw_store_ids = [str(task.store_id)]
        store_uuid_ids: list[UUID] = []
        for sid in raw_store_ids:
            try:
                store_uuid_ids.append(UUID(sid))
            except (ValueError, TypeError):
                continue
        store_name_map: dict = {}
        if store_uuid_ids:
            sres = await db.execute(
                select(Store.id, Store.name).where(Store.id.in_(store_uuid_ids))
            )
            store_name_map = {row.id: row.name for row in sres}
        store_names = [store_name_map.get(uid, "") for uid in store_uuid_ids]
        legacy_store_id = raw_store_ids[0] if raw_store_ids else None
        legacy_store_name = store_names[0] if store_names else None

        # 사용자 이름들을 한 번에 조회 (creator + submitted_by + reviewed_by + assignees)
        uid_set: set = set()
        if task.created_by:
            uid_set.add(task.created_by)
        if task.submitted_by:
            uid_set.add(task.submitted_by)
        if task.reviewed_by:
            uid_set.add(task.reviewed_by)
        for a in task.assignees:
            if a.user_id:
                uid_set.add(a.user_id)
        name_map: dict = {}
        if uid_set:
            u_res = await db.execute(
                select(User.id, User.full_name).where(User.id.in_(uid_set))
            )
            name_map = {row.id: row.full_name for row in u_res}

        # attachments: DB 의 key → url 보충해서 응답
        out_attachments: list[dict] = []
        for a in task.attachments or []:
            d = dict(a) if isinstance(a, dict) else a
            out_attachments.append({
                "key": d.get("key"),
                "url": storage_service.resolve_url(d.get("key")),
                "mime_type": d.get("mime_type"),
                "kind": d.get("kind"),
                "name": d.get("name"),
                "size": d.get("size"),
            })

        return {
            "id": str(task.id),
            "organization_id": str(task.organization_id),
            "store_ids": raw_store_ids,
            "store_names": store_names,
            "store_id": legacy_store_id,
            "store_name": legacy_store_name,
            "title": task.title,
            "description": task.description,
            "priority": task.priority,
            "severity": task.severity,
            "category": task.category,
            "status": task.status,
            "due_date": task.due_date,
            "created_by": str(task.created_by) if task.created_by else None,
            "created_by_name": name_map.get(task.created_by) if task.created_by else None,
            "source_report_id": str(task.source_report_id) if task.source_report_id else None,
            "links": task.links or {},
            "attachments": out_attachments,
            "submitted_at": task.submitted_at,
            "submitted_by": str(task.submitted_by) if task.submitted_by else None,
            "submitted_by_name": name_map.get(task.submitted_by) if task.submitted_by else None,
            "reviewed_at": task.reviewed_at,
            "reviewed_by": str(task.reviewed_by) if task.reviewed_by else None,
            "reviewed_by_name": name_map.get(task.reviewed_by) if task.reviewed_by else None,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "assignees": [
                {
                    "user_id": str(a.user_id) if a.user_id else None,
                    "user_name": name_map.get(a.user_id) if a.user_id else None,
                }
                for a in task.assignees
            ],
        }

    async def build_responses_batch(self, db: AsyncSession, tasks: list[Task]) -> list[dict]:
        return [await self.build_response(db, t) for t in tasks]


task_service: TaskService = TaskService()
