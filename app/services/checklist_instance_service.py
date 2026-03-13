"""체크리스트 인스턴스 서비스 — 체크리스트 인스턴스/완료 비즈니스 로직.

Checklist Instance Service — Business logic for checklist instance management.
Handles instance creation from templates, completion tracking, status updates,
and merged snapshot+completion views.
"""

from datetime import date, datetime, timezone
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.assignment import WorkAssignment
from app.models.checklist import (
    ChecklistCompletion,
    ChecklistCompletionHistory,
    ChecklistInstance,
    ChecklistItemReview,
    ChecklistReviewContent,
    ChecklistReviewHistory,
    ChecklistTemplate,
)
from app.models.organization import Store
from app.models.user import User
from app.repositories.checklist_instance_repository import checklist_instance_repository
from app.services.storage_service import storage_service
from app.utils.exceptions import BadRequestError, ForbiddenError, NotFoundError


class ChecklistInstanceService:
    """체크리스트 인스턴스 서비스.

    Checklist instance service handling creation, completion,
    status management, and response building.
    """

    async def create_instance(
        self,
        db: AsyncSession,
        assignment: WorkAssignment,
        template: ChecklistTemplate | None,
        snapshot: dict,
        total_items: int,
    ) -> ChecklistInstance:
        """근무 배정에 대한 체크리스트 인스턴스를 생성합니다.

        Create a checklist instance for a work assignment.
        Called during assignment creation to create the parallel cl_instances row.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            assignment: 근무 배정 ORM 객체 (Work assignment ORM object)
            template: 원본 체크리스트 템플릿 (Source template, may be None)
            snapshot: JSONB 스냅샷 데이터 (Snapshot data from assignment creation)
            total_items: 총 항목 수 (Total items count)

        Returns:
            ChecklistInstance: 생성된 인스턴스 (Created instance)
        """
        instance: ChecklistInstance = await checklist_instance_repository.create(
            db,
            {
                "organization_id": assignment.organization_id,
                "template_id": template.id if template else None,
                "work_assignment_id": assignment.id,
                "store_id": assignment.store_id,
                "user_id": assignment.user_id,
                "work_date": assignment.work_date,
                "snapshot": snapshot,
                "total_items": total_items,
                "completed_items": 0,
                "status": "pending",
            },
        )
        return instance

    async def get_instances(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        user_id: UUID | None = None,
        work_date: date | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[ChecklistInstance], int]:
        """체크리스트 인스턴스 목록을 필터링하여 페이지네이션 조회합니다.

        List checklist instances with filters and pagination.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            store_id: 매장 UUID 필터, 선택 (Optional store UUID filter)
            user_id: 사용자 UUID 필터, 선택 (Optional user UUID filter)
            work_date: 근무일 필터, 선택 (Optional work date filter)
            status: 상태 필터, 선택 (Optional status filter)
            page: 페이지 번호 (Page number)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[ChecklistInstance], int]: (인스턴스 목록, 전체 개수)
        """
        return await checklist_instance_repository.get_by_filters(
            db, organization_id, store_id, user_id, work_date, status, page, per_page
        )

    async def get_instance(
        self,
        db: AsyncSession,
        instance_id: UUID,
        organization_id: UUID | None = None,
    ) -> ChecklistInstance:
        """체크리스트 인스턴스 상세를 조회합니다 (완료 기록 포함).

        Get checklist instance detail with completions.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            instance_id: 인스턴스 UUID (Instance UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            ChecklistInstance: 인스턴스 상세 (Instance detail with completions)

        Raises:
            NotFoundError: 인스턴스가 없을 때 (When instance not found)
        """
        instance: ChecklistInstance | None = await checklist_instance_repository.get_with_completions(
            db, instance_id, organization_id
        )
        if instance is None:
            raise NotFoundError("체크리스트 인스턴스를 찾을 수 없습니다 (Checklist instance not found)")
        return instance

    async def get_instance_by_assignment(
        self,
        db: AsyncSession,
        work_assignment_id: UUID,
    ) -> ChecklistInstance | None:
        """근무 배정 ID로 인스턴스를 조회합니다.

        Get checklist instance by work assignment ID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            work_assignment_id: 근무 배정 UUID (Work assignment UUID)

        Returns:
            ChecklistInstance | None: 인스턴스 또는 None (Instance or None)
        """
        return await checklist_instance_repository.get_by_assignment_id(db, work_assignment_id)

    async def get_my_instances(
        self,
        db: AsyncSession,
        user_id: UUID,
        work_date: date | None = None,
    ) -> Sequence[ChecklistInstance]:
        """내 체크리스트 인스턴스 목록을 조회합니다 (앱용).

        Get my checklist instances for the app.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)
            work_date: 근무일 필터, 선택 (Optional work date filter)

        Returns:
            Sequence[ChecklistInstance]: 내 인스턴스 목록 (My instance list)
        """
        return await checklist_instance_repository.get_user_instances(db, user_id, work_date)

    async def complete_item(
        self,
        db: AsyncSession,
        instance_id: UUID,
        item_index: int,
        user_id: UUID,
        photo_url: str | None = None,
        note: str | None = None,
        location: dict | None = None,
        client_timezone: str = "America/Los_Angeles",
    ) -> ChecklistInstance:
        """체크리스트 항목을 완료 처리합니다.

        Complete a checklist item in an instance.
        Creates a cl_completion row and updates instance counts/status.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            instance_id: 인스턴스 UUID (Instance UUID)
            item_index: 완료할 항목 인덱스 (Item index to complete)
            user_id: 완료한 사용자 UUID (User who completed the item)
            photo_url: 사진 URL, 선택 (Photo URL, optional)
            note: 메모, 선택 (Note, optional)
            location: GPS 위치, 선택 (Location data, optional)

        Returns:
            ChecklistInstance: 업데이트된 인스턴스 (Updated instance with completions)

        Raises:
            NotFoundError: 인스턴스가 없을 때 (When instance not found)
            ForbiddenError: 다른 사용자의 인스턴스일 때 (When instance belongs to another user)
            BadRequestError: 항목 인덱스 범위 초과 또는 이미 완료된 항목
        """
        # 인스턴스 조회 — Fetch instance with completions
        instance: ChecklistInstance | None = await checklist_instance_repository.get_with_completions(
            db, instance_id
        )
        if instance is None:
            raise NotFoundError("체크리스트 인스턴스를 찾을 수 없습니다 (Checklist instance not found)")

        # 본인 확인 — Verify ownership
        if instance.user_id != user_id:
            raise ForbiddenError("본인의 체크리스트만 완료할 수 있습니다 (Can only complete your own checklist)")

        # 스냅샷 항목 유효성 검증 — Validate item_index against snapshot
        snapshot_items: list[dict] = instance.snapshot.get("items", [])
        if item_index < 0 or item_index >= len(snapshot_items):
            raise BadRequestError(
                f"항목 인덱스가 범위를 벗어났습니다 (Item index out of range: {item_index})"
            )

        # 이미 완료된 항목인지 확인 — Check if already completed
        existing: ChecklistCompletion | None = await checklist_instance_repository.get_completion(
            db, instance_id, item_index
        )
        if existing is not None:
            raise BadRequestError(
                f"이미 완료된 항목입니다 (Item {item_index} is already completed)"
            )

        # 항목 타입별 검증 — Validate required evidence based on verification_type
        target_item: dict = snapshot_items[item_index]
        v_type: str = target_item.get("verification_type", "none")
        if "photo" in v_type and not photo_url:
            raise BadRequestError(
                "이 항목은 사진이 필요합니다 (Photo is required for this item)"
            )
        if "text" in v_type and not note:
            raise BadRequestError(
                "이 항목은 메모가 필요합니다 (Note is required for this item)"
            )

        # 완료 기록 생성 — Create completion record (UTC + IANA timezone)
        await checklist_instance_repository.create_completion(
            db,
            {
                "instance_id": instance_id,
                "item_index": item_index,
                "user_id": user_id,
                "completed_at": datetime.now(timezone.utc),
                "completed_timezone": client_timezone,
                "photo_url": photo_url,
                "note": note,
                "location": location,
            },
        )

        # 완료 항목 수 업데이트 — Update completed count
        new_completed: int = instance.completed_items + 1
        instance.completed_items = new_completed

        # 상태 자동 업데이트 — Auto-update status
        if new_completed == instance.total_items:
            instance.status = "completed"
        elif new_completed > 0:
            instance.status = "in_progress"

        await db.flush()
        await db.refresh(instance)

        # completions 재로드 — Reload with completions
        return await self.get_instance(db, instance_id)

    async def get_review_summary(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> dict:
        """리뷰 요약 통계를 조회합니다.

        Get aggregated review summary counts for a date range.
        """
        return await checklist_instance_repository.get_review_summary(
            db,
            organization_id=organization_id,
            store_id=store_id,
            date_from=date_from,
            date_to=date_to,
        )

    async def get_completion_log(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        user_id: UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list[dict], int]:
        """Get checklist completion log with user and instance info.

        Queries cl_completions joined with cl_instances and users.
        """
        from sqlalchemy import func as sa_func
        from app.models.organization import Store

        # Base query: completions joined with instances
        base_filter = select(ChecklistCompletion).join(
            ChecklistInstance,
            ChecklistCompletion.instance_id == ChecklistInstance.id,
        ).where(ChecklistInstance.organization_id == organization_id)

        if store_id is not None:
            base_filter = base_filter.where(ChecklistInstance.store_id == store_id)
        if user_id is not None:
            base_filter = base_filter.where(ChecklistCompletion.user_id == user_id)
        if date_from is not None:
            base_filter = base_filter.where(ChecklistInstance.work_date >= date_from)
        if date_to is not None:
            base_filter = base_filter.where(ChecklistInstance.work_date <= date_to)

        # Count total
        count_query = select(sa_func.count()).select_from(base_filter.subquery())
        total_result = await db.execute(count_query)
        total: int = total_result.scalar() or 0

        # Fetch paginated completions with instance eager-loaded
        data_query = (
            base_filter
            .options(selectinload(ChecklistCompletion.instance))
            .order_by(ChecklistCompletion.completed_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        result = await db.execute(data_query)
        completions: list[ChecklistCompletion] = list(result.scalars().all())

        # Build response with user/store names
        items: list[dict] = []
        for comp in completions:
            inst: ChecklistInstance = comp.instance

            # Fetch user name
            user_result = await db.execute(select(User.full_name).where(User.id == comp.user_id))
            user_name: str = user_result.scalar() or "Unknown"

            # Fetch store name
            store_result = await db.execute(select(Store.name).where(Store.id == inst.store_id))
            store_name: str = store_result.scalar() or "Unknown"

            # Get item title from snapshot
            snapshot_items: list[dict] = inst.snapshot.get("items", []) if inst.snapshot else []
            item_title: str = "Unknown"
            for s_item in snapshot_items:
                if s_item.get("item_index") == comp.item_index:
                    item_title = s_item.get("title", "Unknown")
                    break

            items.append({
                "id": str(comp.id),
                "instance_id": str(comp.instance_id),
                "item_index": comp.item_index,
                "item_title": item_title,
                "user_id": str(comp.user_id),
                "user_name": user_name,
                "store_id": str(inst.store_id),
                "store_name": store_name,
                "work_date": inst.work_date.isoformat(),
                "completed_at": comp.completed_at.isoformat() if comp.completed_at else None,
                "completed_timezone": comp.completed_timezone,
                "photo_url": comp.photo_url,
                "note": comp.note,
            })

        return items, total

    async def build_response(
        self,
        db: AsyncSession,
        instance: ChecklistInstance,
    ) -> dict:
        """인스턴스 응답 딕셔너리를 구성합니다 (관련 엔티티 이름 포함).

        Build instance response dict with related entity names.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            instance: 체크리스트 인스턴스 ORM 객체 (Checklist instance ORM object)

        Returns:
            dict: 매장/사용자 이름이 포함된 응답 딕셔너리
                  (Response dict with store/user names)
        """
        # 관련 엔티티 이름 조회 — Fetch related entity names
        store_result = await db.execute(select(Store.name).where(Store.id == instance.store_id))
        store_name: str = store_result.scalar() or "Unknown"

        user_result = await db.execute(select(User.full_name).where(User.id == instance.user_id))
        user_name: str = user_result.scalar() or "Unknown"

        return {
            "id": str(instance.id),
            "template_id": str(instance.template_id) if instance.template_id else None,
            "work_assignment_id": str(instance.work_assignment_id),
            "store_id": str(instance.store_id),
            "store_name": store_name,
            "user_id": str(instance.user_id),
            "user_name": user_name,
            "work_date": instance.work_date,
            "total_items": instance.total_items,
            "completed_items": instance.completed_items,
            "status": instance.status,
            "created_at": instance.created_at,
        }

    async def build_detail_response(
        self,
        db: AsyncSession,
        instance: ChecklistInstance,
    ) -> dict:
        """인스턴스 상세 응답 딕셔너리를 구성합니다 (스냅샷 + 완료 기록 병합).

        Build instance detail response dict with snapshot items merged
        with completion data.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            instance: 체크리스트 인스턴스 ORM 객체 (Checklist instance ORM object)

        Returns:
            dict: 병합된 스냅샷이 포함된 상세 응답 딕셔너리
                  (Detail response dict with merged snapshot)
        """
        response: dict = await self.build_response(db, instance)

        # 스냅샷 항목에 완료 정보 병합 — Merge completion data into snapshot items
        snapshot: dict | None = instance.snapshot
        if snapshot and "items" in snapshot:
            # 완료 기록을 item_index로 인덱싱 — Index completions by item_index
            completions_map: dict[int, ChecklistCompletion] = {}
            if hasattr(instance, "completions") and instance.completions:
                for comp in instance.completions:
                    completions_map[comp.item_index] = comp

            # 리뷰 기록을 item_index로 인덱싱 — Index reviews by item_index
            reviews_map: dict[int, ChecklistItemReview] = {}
            if hasattr(instance, "reviews") and instance.reviews:
                for rev in instance.reviews:
                    reviews_map[rev.item_index] = rev

            # 리뷰어+콘텐츠 작성자+히스토리 작성자 이름 일괄 조회 — Bulk fetch user names
            user_name_cache: dict[UUID, str] = {}
            user_ids_to_fetch: set[UUID] = {rev.reviewer_id for rev in reviews_map.values()}
            for rev in reviews_map.values():
                if hasattr(rev, "contents") and rev.contents:
                    for c in rev.contents:
                        user_ids_to_fetch.add(c.author_id)
                if hasattr(rev, "review_history") and rev.review_history:
                    for h in rev.review_history:
                        user_ids_to_fetch.add(h.changed_by)
            if user_ids_to_fetch:
                for uid in user_ids_to_fetch:
                    r = await db.execute(select(User.full_name).where(User.id == uid))
                    user_name_cache[uid] = r.scalar() or "Unknown"

            # 완료자 이름 일괄 조회 — Bulk fetch completer names (reuse cache)
            completer_ids = {comp.user_id for comp in completions_map.values()}
            for cid in completer_ids:
                if cid not in user_name_cache:
                    r = await db.execute(select(User.full_name).where(User.id == cid))
                    user_name_cache[cid] = r.scalar() or "Unknown"

            merged_items: list[dict] = []
            for item in snapshot["items"]:
                item_data: dict[str, Any] = {**item}
                comp: ChecklistCompletion | None = completions_map.get(item["item_index"])
                if comp is not None:
                    item_data["is_completed"] = True
                    item_data["completed_at"] = comp.completed_at.isoformat() if comp.completed_at else None
                    item_data["completed_timezone"] = comp.completed_timezone
                    item_data["completed_tz"] = comp.completed_timezone  # Flutter 앱 호환용 alias
                    item_data["completed_by"] = str(comp.user_id)
                    item_data["completed_by_name"] = user_name_cache.get(comp.user_id)
                    item_data["photo_url"] = comp.photo_url
                    item_data["note"] = comp.note
                    item_data["location"] = comp.location
                else:
                    item_data["is_completed"] = False
                    item_data["completed_at"] = None
                    item_data["completed_timezone"] = None
                    item_data["completed_tz"] = None
                    item_data["completed_by"] = None
                    item_data["completed_by_name"] = None
                    item_data["photo_url"] = None
                    item_data["note"] = None
                    item_data["location"] = None

                # 리뷰 병합 — Merge review data
                rev: ChecklistItemReview | None = reviews_map.get(item["item_index"])
                if rev is not None:
                    contents_list = []
                    if hasattr(rev, "contents") and rev.contents:
                        for c in rev.contents:
                            contents_list.append({
                                "id": str(c.id),
                                "review_id": str(c.review_id),
                                "author_id": str(c.author_id),
                                "author_name": user_name_cache.get(c.author_id),
                                "type": c.type,
                                "content": c.content,
                                "created_at": c.created_at.isoformat(),
                            })
                    history_list = []
                    if hasattr(rev, "review_history") and rev.review_history:
                        for h in rev.review_history:
                            history_list.append({
                                "id": str(h.id),
                                "changed_by": str(h.changed_by),
                                "changed_by_name": user_name_cache.get(h.changed_by),
                                "old_result": h.old_result,
                                "new_result": h.new_result,
                                "created_at": h.created_at.isoformat(),
                            })
                    item_data["review"] = {
                        "id": str(rev.id),
                        "reviewer_id": str(rev.reviewer_id),
                        "reviewer_name": user_name_cache.get(rev.reviewer_id),
                        "result": rev.result,
                        "contents": contents_list,
                        "history": history_list,
                        "created_at": rev.created_at.isoformat(),
                        "updated_at": rev.updated_at.isoformat(),
                    }

                    # 앱용 플랫 필드 — review_status + 개별 반려/승인 필드
                    # review_status: null, "pass", "fail", "caution", "pending_re_review"
                    item_data["review_status"] = rev.result
                    reviewer_name = user_name_cache.get(rev.reviewer_id)

                    # review_history 이벤트별 contents 파티셔닝 — Partition contents by review action
                    # cl_review_contents는 review당 1개 레코드에 모두 연결되므로,
                    # review_history 시간 구간으로 분리하여 각 액션별 코멘트/사진을 정확히 추출
                    sorted_history = sorted(rev.review_history, key=lambda h: h.created_at) if hasattr(rev, "review_history") and rev.review_history else []
                    all_contents = sorted(rev.contents, key=lambda c: c.created_at) if hasattr(rev, "contents") and rev.contents else []

                    def _get_contents_for_history_event(event_idx: int) -> tuple[str | None, list[str]]:
                        """특정 review_history 이벤트에 해당하는 리뷰어 코멘트/사진 추출."""
                        if not sorted_history or event_idx >= len(sorted_history):
                            return None, []
                        event = sorted_history[event_idx]
                        # 이 이벤트 ~ 다음 이벤트 사이에 생성된 콘텐츠
                        event_time = event.created_at
                        next_time = sorted_history[event_idx + 1].created_at if event_idx + 1 < len(sorted_history) else None
                        texts: list[str] = []
                        photos: list[str] = []
                        for c in all_contents:
                            if c.created_at < event_time:
                                continue
                            if next_time is not None and c.created_at >= next_time:
                                break
                            if c.author_id == rev.reviewer_id:
                                if c.type == "text":
                                    texts.append(c.content)
                                elif c.type in ("photo", "video"):
                                    photos.append(c.content)
                        return (texts[-1] if texts else None), photos

                    # 마지막 fail/pass 이벤트의 코멘트/사진 추출 — Find latest event's contents
                    def _find_latest_event_contents(target_result: str) -> tuple[str | None, list[str], str | None, str]:
                        """마지막 target_result 이벤트의 (comment, photos, reviewer_name, at) 반환."""
                        for i in range(len(sorted_history) - 1, -1, -1):
                            if sorted_history[i].new_result == target_result:
                                comment, photos = _get_contents_for_history_event(i)
                                at = sorted_history[i].created_at.isoformat()
                                by = user_name_cache.get(sorted_history[i].changed_by)
                                return comment, photos, by, at
                        return None, [], None, rev.updated_at.isoformat()

                    # 반려 플랫 필드
                    is_rejected = rev.result == "fail"
                    item_data["is_rejected"] = is_rejected
                    if is_rejected:
                        rej_comment, rej_photos, rej_by, rej_at = _find_latest_event_contents("fail")
                        item_data["rejection_comment"] = rej_comment
                        item_data["rejection_photo_urls"] = rej_photos
                        item_data["rejected_by"] = rej_by or reviewer_name
                        item_data["rejected_at"] = rej_at
                    else:
                        item_data["rejection_comment"] = None
                        item_data["rejection_photo_urls"] = []
                        item_data["rejected_by"] = None
                        item_data["rejected_at"] = None

                    # 승인 플랫 필드
                    is_approved = rev.result == "pass"
                    item_data["is_approved"] = is_approved
                    if is_approved:
                        app_comment, app_photos, app_by, app_at = _find_latest_event_contents("pass")
                        item_data["approval_comment"] = app_comment
                        item_data["approval_photo_urls"] = app_photos
                        item_data["approved_by"] = app_by or reviewer_name
                        item_data["approved_at"] = app_at
                    else:
                        item_data["approval_comment"] = None
                        item_data["approval_photo_urls"] = []
                        item_data["approved_by"] = None
                        item_data["approved_at"] = None
                else:
                    item_data["review"] = None
                    item_data["review_status"] = None
                    item_data["is_rejected"] = False
                    item_data["rejection_comment"] = None
                    item_data["rejection_photo_urls"] = []
                    item_data["rejected_by"] = None
                    item_data["rejected_at"] = None
                    item_data["is_approved"] = False
                    item_data["approval_comment"] = None
                    item_data["approval_photo_urls"] = []
                    item_data["approved_by"] = None
                    item_data["approved_at"] = None

                # 완료 히스토리 (재제출 아카이브) 병합
                comp_for_history: ChecklistCompletion | None = completions_map.get(item["item_index"])
                completion_history_list = []
                if comp_for_history is not None and hasattr(comp_for_history, "history") and comp_for_history.history:
                    for ch in comp_for_history.history:
                        completion_history_list.append({
                            "id": str(ch.id),
                            "photo_url": ch.photo_url,
                            "note": ch.note,
                            "location": ch.location,
                            "submitted_at": ch.submitted_at.isoformat(),
                            "created_at": ch.created_at.isoformat(),
                        })
                item_data["completion_history"] = completion_history_list
                item_data["resubmission_count"] = comp_for_history.resubmission_count if comp_for_history else 0

                # 앱용 재제출 응답 필드 — Flat resubmission response fields for Flutter app
                # 현재 반려 상태이면 아직 응답 전이므로 responded_at = None
                is_currently_rejected = item_data.get("is_rejected", False)
                if (
                    not is_currently_rejected
                    and comp_for_history is not None
                    and comp_for_history.resubmission_count
                    and comp_for_history.resubmission_count > 0
                ):
                    item_data["response_comment"] = comp_for_history.note
                    item_data["responded_by"] = user_name_cache.get(comp_for_history.user_id)
                    if completion_history_list:
                        item_data["responded_at"] = completion_history_list[-1]["created_at"]
                    else:
                        item_data["responded_at"] = comp_for_history.completed_at.isoformat() if comp_for_history.completed_at else None
                else:
                    item_data["response_comment"] = None
                    item_data["responded_at"] = None
                    item_data["responded_by"] = None

                # 앱용 타임라인 이벤트 — Build interleaved history from review_history + completion_history
                timeline_events: list[dict] = []
                rev_for_timeline = reviews_map.get(item["item_index"])
                comp_for_timeline = completions_map.get(item["item_index"])

                # 1) 최초 완료 이벤트
                if comp_for_timeline is not None:
                    # 재제출 이력이 있으면 첫 완료 시점은 첫 번째 completion_history의 submitted_at
                    if comp_for_timeline.history:
                        first_archive = comp_for_timeline.history[0]
                        timeline_events.append({
                            "type": "completed",
                            "comment": first_archive.note,
                            "photo_urls": [first_archive.photo_url] if first_archive.photo_url else [],
                            "by": user_name_cache.get(comp_for_timeline.user_id),
                            "at": first_archive.submitted_at.isoformat(),
                        })
                    else:
                        timeline_events.append({
                            "type": "completed",
                            "comment": comp_for_timeline.note,
                            "photo_urls": [comp_for_timeline.photo_url] if comp_for_timeline.photo_url else [],
                            "by": user_name_cache.get(comp_for_timeline.user_id),
                            "at": comp_for_timeline.completed_at.isoformat() if comp_for_timeline.completed_at else None,
                        })

                # 2) review_history + completion_history 이벤트를 시간순 인터리빙
                if rev_for_timeline is not None and hasattr(rev_for_timeline, "review_history") and rev_for_timeline.review_history:
                    tl_history = sorted(rev_for_timeline.review_history, key=lambda h: h.created_at)
                    tl_contents = sorted(rev_for_timeline.contents, key=lambda c: c.created_at) if hasattr(rev_for_timeline, "contents") and rev_for_timeline.contents else []

                    for idx, rh in enumerate(tl_history):
                        event_time = rh.created_at
                        next_time = tl_history[idx + 1].created_at if idx + 1 < len(tl_history) else None

                        # 이 이벤트 시간 윈도우에 해당하는 리뷰어 코멘트/사진 추출
                        rh_comment = None
                        rh_photos: list[str] = []
                        for c in tl_contents:
                            if c.created_at < event_time:
                                continue
                            if next_time is not None and c.created_at >= next_time:
                                break
                            if c.author_id == rev_for_timeline.reviewer_id:
                                if c.type == "text":
                                    rh_comment = c.content
                                elif c.type in ("photo", "video"):
                                    rh_photos.append(c.content)

                        if rh.new_result == "fail":
                            timeline_events.append({
                                "type": "rejected",
                                "comment": rh_comment,
                                "photo_urls": rh_photos,
                                "by": user_name_cache.get(rh.changed_by),
                                "at": rh.created_at.isoformat(),
                            })
                        elif rh.new_result == "pass":
                            timeline_events.append({
                                "type": "approved",
                                "comment": rh_comment,
                                "photo_urls": rh_photos,
                                "by": user_name_cache.get(rh.changed_by),
                                "at": rh.created_at.isoformat(),
                            })
                        elif rh.new_result == "pending_re_review":
                            timeline_events.append({
                                "type": "pending",
                                "comment": None,
                                "photo_urls": [],
                                "by": user_name_cache.get(rh.changed_by),
                                "at": rh.created_at.isoformat(),
                            })

                # 3) 재제출 이벤트 (completion_history의 각 아카이브 = 재제출 시점)
                if comp_for_timeline is not None and comp_for_timeline.history:
                    for i, arch in enumerate(comp_for_timeline.history):
                        # 재제출 시 새 데이터: 다음 아카이브의 원본 또는 현재 completion
                        if i + 1 < len(comp_for_timeline.history):
                            next_arch = comp_for_timeline.history[i + 1]
                            resp_note = next_arch.note
                            resp_photo = [next_arch.photo_url] if next_arch.photo_url else []
                        else:
                            resp_note = comp_for_timeline.note
                            resp_photo = [comp_for_timeline.photo_url] if comp_for_timeline.photo_url else []
                        timeline_events.append({
                            "type": "responded",
                            "comment": resp_note,
                            "photo_urls": resp_photo,
                            "by": user_name_cache.get(comp_for_timeline.user_id),
                            "at": arch.created_at.isoformat(),
                        })

                # 시간순 정렬
                timeline_events.sort(key=lambda e: e.get("at") or "")
                item_data["history"] = timeline_events

                merged_items.append(item_data)

            response["snapshot"] = merged_items
        else:
            response["snapshot"] = None

        return response


    async def upsert_review(
        self,
        db: AsyncSession,
        instance_id: UUID,
        item_index: int,
        reviewer_id: UUID,
        result: str,
        comment_text: str | None = None,
        comment_photo_url: str | None = None,
    ) -> ChecklistItemReview:
        """항목 리뷰를 생성하거나 수정합니다 (upsert).

        Create or update an item review. One review per (instance_id, item_index).
        Records history when result changes. Optionally adds inline comment.
        """
        # 인스턴스 존재 확인 + item_index 범위 검증
        instance = await checklist_instance_repository.get_with_completions(db, instance_id)
        if instance is None:
            raise NotFoundError("체크리스트 인스턴스를 찾을 수 없습니다 (Checklist instance not found)")

        snapshot_items = instance.snapshot.get("items", []) if instance.snapshot else []
        if item_index < 0 or item_index >= len(snapshot_items):
            raise BadRequestError(f"항목 인덱스가 범위를 벗어났습니다 (Item index out of range: {item_index})")

        # 기존 리뷰 조회
        existing = (
            await db.execute(
                select(ChecklistItemReview).where(
                    ChecklistItemReview.instance_id == instance_id,
                    ChecklistItemReview.item_index == item_index,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            # 결과 변경 시 히스토리 기록
            if existing.result != result:
                history = ChecklistReviewHistory(
                    review_id=existing.id,
                    changed_by=reviewer_id,
                    old_result=existing.result,
                    new_result=result,
                )
                db.add(history)
            existing.result = result
            existing.reviewer_id = reviewer_id
            await db.flush()
            await db.refresh(existing)
            review = existing
        else:
            review = ChecklistItemReview(
                instance_id=instance_id,
                item_index=item_index,
                reviewer_id=reviewer_id,
                result=result,
            )
            db.add(review)
            await db.flush()
            await db.refresh(review)
            # 최초 생성 히스토리
            history = ChecklistReviewHistory(
                review_id=review.id,
                changed_by=reviewer_id,
                old_result=None,
                new_result=result,
            )
            db.add(history)
            await db.flush()

        # 인라인 코멘트 추가
        if comment_text:
            rc = ChecklistReviewContent(
                review_id=review.id,
                author_id=reviewer_id,
                type="text",
                content=comment_text,
            )
            db.add(rc)
            await db.flush()

        if comment_photo_url:
            finalized_url = storage_service.finalize_upload(comment_photo_url)
            rc = ChecklistReviewContent(
                review_id=review.id,
                author_id=reviewer_id,
                type="photo",
                content=finalized_url,
            )
            db.add(rc)
            await db.flush()

        return review

    async def resubmit_completion(
        self,
        db: AsyncSession,
        instance_id: UUID,
        item_index: int,
        user_id: UUID,
        photo_url: str | None = None,
        note: str | None = None,
        location: dict | None = None,
        client_timezone: str | None = None,
    ) -> ChecklistInstance:
        """Staff가 완료된 항목을 재제출합니다.

        Archives existing evidence, updates completion with new data,
        sets review to pending_re_review, and notifies the reviewer.
        """
        from app.services.notification_service import notification_service

        instance = await checklist_instance_repository.get_with_completions(db, instance_id)
        if instance is None:
            raise NotFoundError("체크리스트 인스턴스를 찾을 수 없습니다 (Checklist instance not found)")

        if instance.user_id != user_id:
            raise ForbiddenError("본인의 체크리스트만 재제출할 수 있습니다 (Can only resubmit your own checklist)")

        # 완료 기록 조회
        completion = await checklist_instance_repository.get_completion(db, instance_id, item_index)
        if completion is None:
            raise BadRequestError("완료되지 않은 항목은 재제출할 수 없습니다 (Cannot resubmit uncompleted item)")

        # 기존 evidence를 completion_history에 아카이빙
        archive = ChecklistCompletionHistory(
            completion_id=completion.id,
            photo_url=completion.photo_url,
            note=completion.note,
            location=completion.location,
            submitted_at=completion.completed_at,
        )
        db.add(archive)

        # completion 업데이트
        now = datetime.now(timezone.utc)
        if photo_url:
            completion.photo_url = storage_service.finalize_upload(photo_url)
        elif photo_url is not None:
            completion.photo_url = None
        if note is not None:
            completion.note = note
        if location is not None:
            completion.location = location
        completion.completed_at = now
        if client_timezone:
            completion.completed_timezone = client_timezone
        completion.resubmission_count = (completion.resubmission_count or 0) + 1

        await db.flush()

        # 리뷰가 있으면 pending_re_review로 변경
        existing_review = (
            await db.execute(
                select(ChecklistItemReview).where(
                    ChecklistItemReview.instance_id == instance_id,
                    ChecklistItemReview.item_index == item_index,
                )
            )
        ).scalar_one_or_none()

        if existing_review is not None:
            old_result = existing_review.result
            if old_result != "pending_re_review":
                history = ChecklistReviewHistory(
                    review_id=existing_review.id,
                    changed_by=user_id,
                    old_result=old_result,
                    new_result="pending_re_review",
                )
                db.add(history)
            existing_review.result = "pending_re_review"
            await db.flush()

            # reviewer에게 알림
            await notification_service.create_for_checklist_re_review(
                db,
                instance=instance,
                review=existing_review,
            )

        return await self.get_instance(db, instance_id)

    async def delete_review(
        self,
        db: AsyncSession,
        instance_id: UUID,
        item_index: int,
    ) -> None:
        """항목 리뷰를 삭제합니다."""
        existing = (
            await db.execute(
                select(ChecklistItemReview).where(
                    ChecklistItemReview.instance_id == instance_id,
                    ChecklistItemReview.item_index == item_index,
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            raise NotFoundError("리뷰를 찾을 수 없습니다 (Review not found)")

        await db.delete(existing)
        await db.flush()

    async def get_reviews_for_instance(
        self,
        db: AsyncSession,
        instance_id: UUID,
    ) -> list[dict]:
        """인스턴스의 모든 리뷰를 조회합니다."""
        result = await db.execute(
            select(ChecklistItemReview)
            .where(ChecklistItemReview.instance_id == instance_id)
            .options(selectinload(ChecklistItemReview.contents))
            .order_by(ChecklistItemReview.item_index)
        )
        reviews = list(result.scalars().all())

        # 이름 캐시
        name_cache: dict[UUID, str] = {}
        async def get_name(uid: UUID) -> str:
            if uid not in name_cache:
                r = await db.execute(select(User.full_name).where(User.id == uid))
                name_cache[uid] = r.scalar() or "Unknown"
            return name_cache[uid]

        items: list[dict] = []
        for rev in reviews:
            contents_list = []
            for c in rev.contents:
                contents_list.append({
                    "id": str(c.id),
                    "review_id": str(c.review_id),
                    "author_id": str(c.author_id),
                    "author_name": await get_name(c.author_id),
                    "type": c.type,
                    "content": c.content,
                    "created_at": c.created_at,
                })
            items.append({
                "id": str(rev.id),
                "instance_id": str(rev.instance_id),
                "item_index": rev.item_index,
                "reviewer_id": str(rev.reviewer_id),
                "reviewer_name": await get_name(rev.reviewer_id),
                "result": rev.result,
                "contents": contents_list,
                "created_at": rev.created_at,
                "updated_at": rev.updated_at,
            })
        return items

    async def add_review_content(
        self,
        db: AsyncSession,
        instance_id: UUID,
        item_index: int,
        author_id: UUID,
        content_type: str,
        content: str,
    ) -> ChecklistReviewContent:
        """리뷰에 콘텐츠(텍스트/사진/영상)를 추가합니다."""
        # 리뷰 존재 확인
        review = (
            await db.execute(
                select(ChecklistItemReview).where(
                    ChecklistItemReview.instance_id == instance_id,
                    ChecklistItemReview.item_index == item_index,
                )
            )
        ).scalar_one_or_none()

        if review is None:
            raise NotFoundError("리뷰를 찾을 수 없습니다 (Review not found)")

        # 미디어 URL이면 finalize
        if content_type in ("photo", "video"):
            content = storage_service.finalize_upload(content)

        rc = ChecklistReviewContent(
            review_id=review.id,
            author_id=author_id,
            type=content_type,
            content=content,
        )
        db.add(rc)
        await db.flush()
        await db.refresh(rc)
        return rc

    async def delete_review_content(
        self,
        db: AsyncSession,
        content_id: UUID,
    ) -> None:
        """리뷰 콘텐츠를 삭제합니다."""
        existing = (
            await db.execute(
                select(ChecklistReviewContent).where(ChecklistReviewContent.id == content_id)
            )
        ).scalar_one_or_none()

        if existing is None:
            raise NotFoundError("콘텐츠를 찾을 수 없습니다 (Content not found)")

        # photo/video 콘텐츠인 경우 S3/로컬 파일 삭제
        if existing.type in ("photo", "video"):
            storage_service.delete_file(existing.content)

        await db.delete(existing)
        await db.flush()


# 싱글턴 인스턴스 — Singleton instance
checklist_instance_service: ChecklistInstanceService = ChecklistInstanceService()
