"""체크리스트 인스턴스 서비스 — 체크리스트 인스턴스/완료 비즈니스 로직.

Checklist Instance Service — Business logic for checklist instance management.
Handles instance creation from templates, item completion tracking, status updates,
and response building from cl_instance_items.
"""

from datetime import date, datetime, timezone
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.checklist import (
    ChecklistInstance,
    ChecklistInstanceItem,
    ChecklistItemFile,
    ChecklistItemMessage,
    ChecklistItemReviewLog,
    ChecklistItemSubmission,
    ChecklistTemplate,
    ClScoreHistory,
)
from app.models.organization import Store
from app.models.user import User
from app.repositories.checklist_instance_repository import checklist_instance_repository
from app.services.storage_service import storage_service
from app.config import settings
from app.utils.exceptions import BadRequestError, ForbiddenError, NotFoundError

# URL 해석 단축 — resolve_url alias for response building
_resolve = storage_service.resolve_url


class ChecklistInstanceService:
    """체크리스트 인스턴스 서비스.

    Checklist instance service handling creation, completion,
    status management, and response building.
    """

    async def create_for_schedule(
        self,
        db: AsyncSession,
        schedule_id: UUID,
        organization_id: UUID,
        store_id: UUID,
        user_id: UUID,
        work_date: date,
        work_role_id: UUID | None,
    ) -> ChecklistInstance | None:
        """스케줄에 대한 체크리스트 인스턴스를 자동 생성합니다.

        work_role의 default_checklist_id에서 템플릿을 찾아 cl_instance_items 행을 생성.
        템플릿이 없으면 None 반환 (체크리스트 없는 스케줄).
        """
        if not work_role_id:
            return None

        from app.models.schedule import StoreWorkRole

        wr_result = await db.execute(
            select(StoreWorkRole).where(StoreWorkRole.id == work_role_id)
        )
        wr = wr_result.scalar_one_or_none()
        if not wr or not wr.default_checklist_id:
            return None

        # 템플릿 + 아이템 로드
        template_result = await db.execute(
            select(ChecklistTemplate)
            .options(selectinload(ChecklistTemplate.items))
            .where(ChecklistTemplate.id == wr.default_checklist_id)
        )
        template = template_result.scalar_one_or_none()
        if not template or not template.items:
            return None

        sorted_items = sorted(template.items, key=lambda x: x.sort_order)

        # 인스턴스 생성
        instance = await checklist_instance_repository.create(
            db,
            {
                "organization_id": organization_id,
                "template_id": template.id,
                "schedule_id": schedule_id,
                "store_id": store_id,
                "user_id": user_id,
                "work_date": work_date,
                "total_items": len(sorted_items),
                "completed_items": 0,
                "status": "pending",
            },
        )

        # cl_instance_items 생성 (템플릿 스냅샷)
        for idx, item in enumerate(sorted_items):
            ii = ChecklistInstanceItem(
                instance_id=instance.id,
                item_index=idx,
                title=item.title,
                description=item.description,
                verification_type=item.verification_type,
                sort_order=item.sort_order,
                is_completed=False,
            )
            db.add(ii)

        await db.flush()
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
        """체크리스트 인스턴스 목록을 필터링하여 페이지네이션 조회합니다."""
        return await checklist_instance_repository.get_by_filters(
            db, organization_id, store_id, user_id, work_date, status, page, per_page
        )

    async def get_instance(
        self,
        db: AsyncSession,
        instance_id: UUID,
        organization_id: UUID | None = None,
    ) -> ChecklistInstance:
        """체크리스트 인스턴스 상세를 조회합니다 (아이템 포함).

        Raises:
            NotFoundError: 인스턴스가 없을 때
        """
        instance: ChecklistInstance | None = await checklist_instance_repository.get_with_items(
            db, instance_id, organization_id
        )
        if instance is None:
            raise NotFoundError("Checklist instance not found")
        return instance

    async def get_my_instances(
        self,
        db: AsyncSession,
        user_id: UUID,
        work_date: date | None = None,
    ) -> Sequence[ChecklistInstance]:
        """내 체크리스트 인스턴스 목록을 조회합니다 (앱용)."""
        return await checklist_instance_repository.get_user_instances(db, user_id, work_date)

    async def complete_item(
        self,
        db: AsyncSession,
        instance_id: UUID,
        item_index: int,
        user_id: UUID,
        photo_url: str | None = None,
        photo_urls: list[str] | None = None,
        note: str | None = None,
        location: dict | None = None,
        client_timezone: str = "America/Los_Angeles",
    ) -> ChecklistInstance:
        """체크리스트 항목을 완료 처리합니다.

        Updates cl_instance_items row and creates cl_item_files + cl_item_submissions.
        Accepts photo_urls (list, preferred) or photo_url (single, backward compat).

        Raises:
            NotFoundError: 인스턴스가 없을 때
            ForbiddenError: 다른 사용자의 인스턴스일 때
            BadRequestError: 항목 인덱스 범위 초과 또는 이미 완료된 항목
        """
        instance: ChecklistInstance | None = await checklist_instance_repository.get_with_items(
            db, instance_id
        )
        if instance is None:
            raise NotFoundError("Checklist instance not found")

        if instance.user_id != user_id:
            raise ForbiddenError("Can only complete your own checklist")

        # 항목 조회
        target_item: ChecklistInstanceItem | None = next(
            (it for it in instance.items if it.item_index == item_index), None
        )
        if target_item is None:
            raise BadRequestError(
                f"Item index out of range: {item_index}"
            )

        if target_item.is_completed:
            # 멱등성: 이미 완료된 항목이면 에러 대신 현재 상태 반환
            return await self.get_instance(db, instance_id)

        # Resolve effective photo list: photo_urls takes priority over single photo_url
        effective_photo_urls: list[str] = []
        if photo_urls:
            effective_photo_urls = photo_urls
        elif photo_url:
            effective_photo_urls = [photo_url]

        # 항목 타입별 검증
        v_type: str = target_item.verification_type or "none"
        if "photo" in v_type and not effective_photo_urls:
            raise BadRequestError("Photo is required for this item")
        if "text" in v_type and not note:
            raise BadRequestError("Note is required for this item")

        try:
            now = datetime.now(timezone.utc)

            # 완료 데이터 업데이트
            target_item.is_completed = True
            target_item.completed_at = now
            target_item.completed_tz = client_timezone
            target_item.completed_by = user_id

            # 제출 이력 기록 (항상 생성 — 사진 유무 관계없이)
            submission = ChecklistItemSubmission(
                item_id=target_item.id,
                version=1,
                note=note,
                location=location,
                submitted_by=user_id,
                submitted_at=now,
            )
            db.add(submission)
            await db.flush()

            # 파일 저장 — one row per photo
            for idx, p_url in enumerate(effective_photo_urls):
                finalized = storage_service.finalize_upload(p_url)
                file_row = ChecklistItemFile(
                    item_id=target_item.id,
                    context="submission",
                    context_id=submission.id,
                    file_url=finalized,
                    file_type="photo",
                    sort_order=idx,
                    uploaded_by=user_id,
                )
                db.add(file_row)

            await db.flush()

            # 완료 항목 수 업데이트
            new_completed: int = instance.completed_items + 1
            instance.completed_items = new_completed
            if new_completed == instance.total_items:
                instance.status = "completed"
            elif new_completed > 0:
                instance.status = "in_progress"

            await db.flush()
            await db.refresh(instance)

            result = await self.get_instance(db, instance_id)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def uncomplete_item(
        self,
        db: AsyncSession,
        instance_id: UUID,
        item_index: int,
        user_id: UUID,
    ) -> ChecklistInstance:
        """체크리스트 항목 완료를 취소합니다."""
        instance: ChecklistInstance | None = await checklist_instance_repository.get_with_items(
            db, instance_id
        )
        if instance is None:
            raise NotFoundError("Checklist instance not found")

        if instance.user_id != user_id:
            raise ForbiddenError("Can only modify your own checklist")

        target_item: ChecklistInstanceItem | None = next(
            (it for it in instance.items if it.item_index == item_index), None
        )
        if target_item is None or not target_item.is_completed:
            raise BadRequestError("Item is not completed")

        try:
            # 완료 상태 초기화
            target_item.is_completed = False
            target_item.completed_at = None
            target_item.completed_tz = None
            target_item.completed_by = None

            await db.flush()

            new_completed: int = max(instance.completed_items - 1, 0)
            instance.completed_items = new_completed
            if new_completed == 0:
                instance.status = "pending"
            elif new_completed < instance.total_items:
                instance.status = "in_progress"

            await db.flush()
            await db.refresh(instance)

            result = await self.get_instance(db, instance_id)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def resubmit_completion(
        self,
        db: AsyncSession,
        instance_id: UUID,
        item_index: int,
        user_id: UUID,
        photo_url: str | None = None,
        photo_urls: list[str] | None = None,
        note: str | None = None,
        location: dict | None = None,
        client_timezone: str | None = None,
    ) -> ChecklistInstance:
        """Staff가 완료된 항목을 재제출합니다.

        Archives existing evidence into cl_item_submissions,
        updates item with new data, sets review_result to pending_re_review.
        """
        from app.services.notification_service import notification_service

        instance = await checklist_instance_repository.get_with_items(db, instance_id)
        if instance is None:
            raise NotFoundError("Checklist instance not found")

        if instance.user_id != user_id:
            raise ForbiddenError("Can only resubmit your own checklist")

        target_item: ChecklistInstanceItem | None = next(
            (it for it in instance.items if it.item_index == item_index), None
        )
        if target_item is None:
            raise BadRequestError("Item not found")
        # fail 리뷰를 받은 항목은 미완료여도 재제출 허용 (GM이 미완료 항목에 fail 줄 수 있음)
        if not target_item.is_completed and target_item.review_result != "fail":
            raise BadRequestError("Cannot resubmit an uncompleted item")

        try:
            now = datetime.now(timezone.utc)

            # 미완료 상태였으면 완료 처리
            was_uncompleted = not target_item.is_completed
            if was_uncompleted:
                target_item.is_completed = True
                target_item.completed_at = now
                target_item.completed_by = user_id
                instance.completed_items = min(
                    (instance.completed_items or 0) + 1, instance.total_items
                )
                if instance.completed_items >= instance.total_items:
                    instance.status = "completed"
                elif instance.completed_items > 0:
                    instance.status = "in_progress"

            # 기존 submission 개수로 새 version 계산
            existing_version_count = len(target_item.submissions) if target_item.submissions else 0
            new_version = existing_version_count + 1

            # 새 제출 이력 생성
            new_submission = ChecklistItemSubmission(
                item_id=target_item.id,
                version=new_version,
                note=note,
                location=location,
                submitted_by=user_id,
                submitted_at=now,
            )
            db.add(new_submission)
            await db.flush()

            # item 완료 시간 갱신
            target_item.completed_at = now
            if client_timezone:
                target_item.completed_tz = client_timezone

            # 새 파일 추가 (photo_urls 우선, photo_url fallback)
            effective_urls = photo_urls or ([photo_url] if photo_url else [])
            for idx, url in enumerate(effective_urls):
                finalized = storage_service.finalize_upload(url)
                if finalized:
                    new_file = ChecklistItemFile(
                        item_id=target_item.id,
                        context="submission",
                        context_id=new_submission.id,
                        file_url=finalized,
                        file_type="photo",
                        uploaded_by=user_id,
                        sort_order=idx,
                    )
                    db.add(new_file)

            await db.flush()

            # 재제출 시 reported_at 리셋 → 다시 Submit Report 가능
            instance.reported_at = None

            # 리뷰가 있으면 pending_re_review로 변경
            if target_item.review_result is not None and target_item.review_result != "pending_re_review":
                old_result = target_item.review_result
                target_item.review_result = "pending_re_review"
                target_item.reviewed_at = now

                log = ChecklistItemReviewLog(
                    item_id=target_item.id,
                    old_result=old_result,
                    new_result="pending_re_review",
                    changed_by=user_id,
                )
                db.add(log)
                await db.flush()

                # reviewer에게 알림 (기존 인터페이스와 호환성 유지)
                await notification_service.create_for_checklist_re_review_item(
                    db,
                    instance=instance,
                    item=target_item,
                )

            result = await self.get_instance(db, instance_id)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise

    async def upsert_review(
        self,
        db: AsyncSession,
        instance_id: UUID,
        item_index: int,
        reviewer_id: UUID,
        result: str,
        comment_text: str | None = None,
        comment_photo_url: str | None = None,
    ) -> ChecklistInstanceItem:
        """항목 리뷰를 생성하거나 수정합니다 (upsert).

        Updates review_result on the instance item and logs the change.
        Optionally adds an inline comment message.
        """
        instance = await checklist_instance_repository.get_with_items(db, instance_id)
        if instance is None:
            raise NotFoundError("Checklist instance not found")

        target_item: ChecklistInstanceItem | None = next(
            (it for it in instance.items if it.item_index == item_index), None
        )
        if target_item is None:
            raise BadRequestError(f"Item index out of range: {item_index}")

        now = datetime.now(timezone.utc)

        # 동일 결과 중복 방지 (pass → pass, fail → fail)
        old_result = target_item.review_result
        if old_result == result:
            raise BadRequestError(f"Already reviewed as {result}")

        try:
            # 결과 변경 이력 기록 (항상)
            log = ChecklistItemReviewLog(
                item_id=target_item.id,
                old_result=old_result,
                new_result=result,
                comment=comment_text,
                changed_by=reviewer_id,
            )
            db.add(log)
            await db.flush()

            # 리뷰 피드백 사진이 있으면 cl_item_files에 저장
            if comment_photo_url:
                finalized = storage_service.finalize_upload(comment_photo_url)
                file_row = ChecklistItemFile(
                    item_id=target_item.id,
                    context="review",
                    context_id=log.id,
                    file_url=finalized,
                    file_type="photo",
                    uploaded_by=reviewer_id,
                )
                db.add(file_row)

            target_item.review_result = result
            target_item.reviewer_id = reviewer_id
            target_item.reviewed_at = now
            await db.flush()
            await db.refresh(target_item)

            await db.commit()
            return target_item
        except Exception:
            await db.rollback()
            raise

    async def delete_review(
        self,
        db: AsyncSession,
        instance_id: UUID,
        item_index: int,
        reviewer_id: UUID,
    ) -> None:
        """항목 리뷰를 취소합니다. 이력에 old_result → NULL로 기록."""
        instance = await checklist_instance_repository.get_with_items(db, instance_id)
        if instance is None:
            raise NotFoundError("Checklist instance not found")

        target_item: ChecklistInstanceItem | None = next(
            (it for it in instance.items if it.item_index == item_index), None
        )
        if target_item is None or target_item.review_result is None:
            raise NotFoundError("Review not found")

        try:
            # 취소 이력 기록
            log = ChecklistItemReviewLog(
                item_id=target_item.id,
                old_result=target_item.review_result,
                new_result=None,
                changed_by=reviewer_id,
            )
            db.add(log)

            target_item.review_result = None
            target_item.reviewer_id = None
            target_item.reviewed_at = None
            await db.flush()
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def get_reviews_for_instance(
        self,
        db: AsyncSession,
        instance_id: UUID,
    ) -> list[dict]:
        """인스턴스의 모든 리뷰 정보를 조회합니다 (review_result 있는 항목만)."""
        result = await db.execute(
            select(ChecklistInstanceItem)
            .where(
                ChecklistInstanceItem.instance_id == instance_id,
                ChecklistInstanceItem.review_result.isnot(None),
            )
            .options(
                selectinload(ChecklistInstanceItem.messages),
                selectinload(ChecklistInstanceItem.reviews_log),
            )
            .order_by(ChecklistInstanceItem.item_index)
        )
        items = list(result.scalars().all())

        name_cache: dict[UUID, str] = {}

        async def get_name(uid: UUID | None) -> str:
            if uid is None:
                return "Unknown"
            if uid not in name_cache:
                r = await db.execute(select(User.full_name).where(User.id == uid))
                name_cache[uid] = r.scalar() or "Unknown"
            return name_cache[uid]

        result_list: list[dict] = []
        for item in items:
            contents_list = []
            for msg in (item.messages or []):
                contents_list.append({
                    "id": str(msg.id),
                    "author_id": str(msg.author_id) if msg.author_id else None,
                    "author_name": await get_name(msg.author_id),
                    "content": msg.content,
                    "created_at": msg.created_at,
                })
            result_list.append({
                "id": str(item.id),
                "instance_id": str(item.instance_id),
                "item_index": item.item_index,
                "reviewer_id": str(item.reviewer_id),
                "reviewer_name": await get_name(item.reviewer_id),
                "result": item.review_result,
                "contents": contents_list,
                "history": [],
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            })
        return result_list

    async def add_review_content(
        self,
        db: AsyncSession,
        instance_id: UUID,
        item_index: int,
        author_id: UUID,
        content_type: str,
        content: str,
    ) -> ChecklistItemMessage:
        """리뷰에 메시지(텍스트/사진/영상)를 추가합니다."""
        instance = await checklist_instance_repository.get_with_items(db, instance_id)
        if instance is None:
            raise NotFoundError("Checklist instance not found")

        target_item: ChecklistInstanceItem | None = next(
            (it for it in instance.items if it.item_index == item_index), None
        )
        if target_item is None or target_item.review_result is None:
            raise NotFoundError("Review not found")

        try:
            if content_type in ("photo", "video"):
                # photo/video: finalize upload → save as cl_item_file with context='chat'
                file_key = storage_service.finalize_upload(content)
                msg = ChecklistItemMessage(
                    item_id=target_item.id,
                    author_id=author_id,
                    content=None,  # no text, just file
                )
                db.add(msg)
                await db.flush()
                await db.refresh(msg)
                file_record = ChecklistItemFile(
                    item_id=target_item.id,
                    context="chat",
                    context_id=msg.id,
                    file_url=file_key,
                    file_type=content_type,
                    sort_order=0,
                )
                db.add(file_record)
            else:
                # text message
                msg = ChecklistItemMessage(
                    item_id=target_item.id,
                    author_id=author_id,
                    content=content,
                )
                db.add(msg)
                await db.flush()
                await db.refresh(msg)

            # compatibility shim: attach aliases for router response
            msg.review_id = target_item.id  # type: ignore[attr-defined]
            msg.type = content_type  # type: ignore[attr-defined]
            if content_type in ("photo", "video"):
                msg.content = storage_service.resolve_url(file_key)  # type: ignore[assignment]
            await db.commit()
            return msg
        except Exception:
            await db.rollback()
            raise

    async def delete_review_content(
        self,
        db: AsyncSession,
        content_id: UUID,
    ) -> None:
        """메시지를 삭제합니다."""
        existing = (
            await db.execute(
                select(ChecklistItemMessage).where(ChecklistItemMessage.id == content_id)
            )
        ).scalar_one_or_none()

        if existing is None:
            raise NotFoundError("Content not found")

        try:
            # Delete associated files (context="chat", context_id=message.id)
            related_files = (
                await db.execute(
                    select(ChecklistItemFile).where(
                        ChecklistItemFile.context == "chat",
                        ChecklistItemFile.context_id == existing.id,
                    )
                )
            ).scalars().all()
            for f in related_files:
                storage_service.delete_file(f.file_url)
                await db.delete(f)

            await db.delete(existing)
            await db.flush()
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def get_review_summary(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> dict:
        """리뷰 요약 통계를 조회합니다."""
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

        Queries cl_instance_items joined with cl_instances and users.
        """
        from sqlalchemy import func as sa_func

        # Base: completed items joined with instances
        base_filter = (
            select(ChecklistInstanceItem)
            .join(ChecklistInstance, ChecklistInstanceItem.instance_id == ChecklistInstance.id)
            .where(
                ChecklistInstance.organization_id == organization_id,
                ChecklistInstanceItem.is_completed.is_(True),
            )
        )

        if store_id is not None:
            base_filter = base_filter.where(ChecklistInstance.store_id == store_id)
        if user_id is not None:
            base_filter = base_filter.where(ChecklistInstanceItem.completed_by == user_id)
        if date_from is not None:
            base_filter = base_filter.where(ChecklistInstance.work_date >= date_from)
        if date_to is not None:
            base_filter = base_filter.where(ChecklistInstance.work_date <= date_to)

        count_query = select(sa_func.count()).select_from(base_filter.subquery())
        total_result = await db.execute(count_query)
        total: int = total_result.scalar() or 0

        data_query = (
            base_filter
            .options(
                selectinload(ChecklistInstanceItem.instance),
                selectinload(ChecklistInstanceItem.submissions),
                selectinload(ChecklistInstanceItem.files),
            )
            .order_by(ChecklistInstanceItem.completed_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        result = await db.execute(data_query)
        items_orm: list[ChecklistInstanceItem] = list(result.scalars().all())

        # Batch lookup: collect unique user_ids and store_ids to avoid N+1
        user_ids: set[UUID] = set()
        store_ids: set[UUID] = set()
        for item in items_orm:
            if item.completed_by:
                user_ids.add(item.completed_by)
            if item.instance and item.instance.store_id:
                store_ids.add(item.instance.store_id)

        user_name_map: dict[UUID, str] = {}
        if user_ids:
            user_rows = await db.execute(
                select(User.id, User.full_name).where(User.id.in_(user_ids))
            )
            user_name_map = {row.id: row.full_name or "Unknown" for row in user_rows}

        store_name_map: dict[UUID, str] = {}
        if store_ids:
            store_rows = await db.execute(
                select(Store.id, Store.name).where(Store.id.in_(store_ids))
            )
            store_name_map = {row.id: row.name or "Unknown" for row in store_rows}

        items: list[dict] = []
        for item in items_orm:
            inst: ChecklistInstance = item.instance

            user_name = user_name_map.get(item.completed_by, "Unknown") if item.completed_by else "Unknown"
            store_name = store_name_map.get(inst.store_id, "Unknown") if inst.store_id else "Unknown"

            # 최신 submission
            log_latest_sub = sorted(item.submissions, key=lambda s: s.version)[-1] if item.submissions else None

            # 현재 파일 URL (최신 파일)
            photo_url: str | None = None
            if item.files:
                latest_file = sorted(item.files, key=lambda f: f.created_at)[-1]
                photo_url = _resolve(latest_file.file_url)

            items.append({
                "id": str(item.id),
                "instance_id": str(item.instance_id),
                "item_index": item.item_index,
                "item_title": item.title,
                "user_id": str(item.completed_by),
                "user_name": user_name,
                "store_id": str(inst.store_id),
                "store_name": store_name,
                "work_date": inst.work_date.isoformat(),
                "completed_at": item.completed_at.isoformat() if item.completed_at else None,
                "completed_timezone": item.completed_tz,
                "photo_url": photo_url,
                "note": log_latest_sub.note if log_latest_sub else None,
            })

        return items, total

    async def update_score(
        self,
        db: AsyncSession,
        instance_id: UUID,
        organization_id: UUID,
        scorer_id: UUID,
        score: int,
        score_note: str | None = None,
    ) -> ChecklistInstance:
        """인스턴스에 점수를 부여하거나 수정합니다. 변경 이력을 cl_score_history에 기록합니다."""
        instance = await self.get_instance(db, instance_id, organization_id)

        now = datetime.now(timezone.utc)

        try:
            history = ClScoreHistory(
                instance_id=instance.id,
                old_score=instance.score,
                new_score=score,
                old_score_note=instance.score_note,
                new_score_note=score_note,
                changed_by=scorer_id,
            )
            db.add(history)

            instance.score = score
            instance.score_note = score_note
            instance.scored_by = scorer_id
            instance.scored_at = now

            await db.flush()
            await db.refresh(instance)
            await db.commit()
            return instance
        except Exception:
            await db.rollback()
            raise

    async def bulk_review(
        self,
        db: AsyncSession,
        instance_id: UUID,
        organization_id: UUID,
        reviewer_id: UUID,
        item_indexes: list[int],
        result: str,
    ) -> list[ChecklistInstanceItem]:
        """여러 항목에 리뷰 결과를 일괄 적용합니다. 이미 동일 결과인 항목은 건너뜁니다."""
        reviewed: list[ChecklistInstanceItem] = []
        for idx in item_indexes:
            try:
                item = await self.upsert_review(
                    db,
                    instance_id=instance_id,
                    item_index=idx,
                    reviewer_id=reviewer_id,
                    result=result,
                )
                reviewed.append(item)
            except BadRequestError:
                # 이미 동일 결과인 항목은 건너뜀
                continue
        return reviewed

    async def build_response(
        self,
        db: AsyncSession,
        instance: ChecklistInstance,
    ) -> dict:
        """인스턴스 응답 딕셔너리를 구성합니다 (관련 엔티티 이름 포함)."""
        store_result = await db.execute(select(Store.name).where(Store.id == instance.store_id))
        store_name: str = store_result.scalar() or "Unknown"

        user_result = await db.execute(select(User.full_name).where(User.id == instance.user_id))
        user_name: str = user_result.scalar() or "Unknown"

        return {
            "id": str(instance.id),
            "template_id": str(instance.template_id) if instance.template_id else None,
            "schedule_id": str(instance.schedule_id) if instance.schedule_id else None,
            "store_id": str(instance.store_id),
            "store_name": store_name,
            "user_id": str(instance.user_id),
            "user_name": user_name,
            "work_date": instance.work_date,
            "total_items": instance.total_items,
            "completed_items": instance.completed_items,
            "status": instance.status,
            "reported_at": instance.reported_at,
            "created_at": instance.created_at,
        }

    async def submit_report(
        self,
        db: AsyncSession,
        instance_id: UUID,
        user_id: UUID,
    ) -> ChecklistInstance:
        """체크리스트 완료 보고 — 해당 store SV/GM에게 알림 + 이메일 발송.

        모든 항목이 완료된 상태에서만 호출 가능.
        """
        instance = await checklist_instance_repository.get_with_items(db, instance_id)
        if instance is None:
            raise NotFoundError("Checklist instance not found")
        if instance.user_id != user_id:
            raise ForbiddenError("Only the assigned user can submit the report")
        if instance.completed_items < instance.total_items:
            raise BadRequestError("Not all items are completed")

        # record report submission time
        instance.reported_at = datetime.now(timezone.utc)

        # staff info
        staff_result = await db.execute(
            select(User).where(User.id == instance.user_id)
        )
        staff = staff_result.scalar_one_or_none()
        staff_name = staff.full_name if staff else "Unknown"
        # work role name (shift - position) from schedule
        work_role_name = ""
        if instance.schedule_id:
            from app.models.schedule import Schedule, StoreWorkRole
            from app.models.work import Shift, Position
            sched_result = await db.execute(
                select(Schedule.work_role_id).where(Schedule.id == instance.schedule_id)
            )
            work_role_id = sched_result.scalar()
            if work_role_id:
                wr_result = await db.execute(
                    select(StoreWorkRole).where(StoreWorkRole.id == work_role_id)
                )
                wr = wr_result.scalar_one_or_none()
                if wr:
                    if wr.name:
                        work_role_name = wr.name
                    else:
                        # name이 비어있으면 shift - position 조합
                        sh_result = await db.execute(select(Shift.name).where(Shift.id == wr.shift_id))
                        pos_result = await db.execute(select(Position.name).where(Position.id == wr.position_id))
                        shift_name = sh_result.scalar() or ""
                        pos_name = pos_result.scalar() or ""
                        work_role_name = f"{shift_name} - {pos_name}".strip(" - ")

        store_result = await db.execute(select(Store.name).where(Store.id == instance.store_id))
        store_name = store_result.scalar() or "Unknown"

        template_name = ""
        if instance.template_id:
            tpl_result = await db.execute(
                select(ChecklistTemplate.title).where(ChecklistTemplate.id == instance.template_id)
            )
            template_name = tpl_result.scalar() or ""

        # notification + get managers for email
        from app.services.notification_service import notification_service
        notifications, managers = await notification_service.create_for_checklist_submitted(
            db, instance, staff_name, store_name
        )
        await db.commit()

        # send email (background, don't block response)
        import asyncio
        from app.utils.email import send_email
        from app.utils.email_templates import build_checklist_completed_email

        work_date_str = instance.work_date.isoformat() if instance.work_date else ""
        admin_url = f"{settings.ADMIN_BASE_URL}/schedules/{instance.schedule_id}"

        for manager in managers:
            if manager.email:
                try:
                    subject, html = build_checklist_completed_email(
                        store_name=store_name,
                        staff_name=staff_name,
                        work_role_name=work_role_name,
                        work_date=work_date_str,
                        template_name=template_name,
                        total_items=instance.total_items,
                        completed_items=instance.completed_items,
                        admin_url=admin_url,
                    )
                    asyncio.create_task(send_email(to=manager.email, subject=subject, html=html))
                except Exception:
                    pass  # email failure should not block

        return instance

    async def build_detail_response(
        self,
        db: AsyncSession,
        instance: ChecklistInstance,
    ) -> dict:
        """인스턴스 상세 응답 — 새 형식 (items + files + submissions + reviews_log + messages).

        가이드 문서 AFTER 형식에 맞춤. 타임라인은 프론트에서 합성.
        """
        response: dict = await self.build_response(db, instance)

        if not instance.items:
            response["items"] = []
            return response

        # 이름 캐시 구성
        user_name_cache: dict[UUID, str] = {}
        user_ids: set[UUID] = set()
        for item in instance.items:
            if item.completed_by:
                user_ids.add(item.completed_by)
            if item.reviewer_id:
                user_ids.add(item.reviewer_id)
            for msg in (item.messages or []):
                if msg.author_id:
                    user_ids.add(msg.author_id)
            for log in (item.reviews_log or []):
                if log.changed_by:
                    user_ids.add(log.changed_by)
            for sub in (item.submissions or []):
                if sub.submitted_by:
                    user_ids.add(sub.submitted_by)

        for uid in user_ids:
            r = await db.execute(select(User.full_name).where(User.id == uid))
            user_name_cache[uid] = r.scalar() or "Unknown"

        items_list: list[dict] = []
        for item in sorted(instance.items, key=lambda i: i.sort_order):
            item_data: dict[str, Any] = {
                "id": str(item.id),
                "item_index": item.item_index,
                "title": item.title,
                "description": item.description,
                "verification_type": item.verification_type,
                "min_photos": item.min_photos,
                "max_photos": item.max_photos,
                "sort_order": item.sort_order,
                # completion
                "is_completed": item.is_completed,
                "completed_at": item.completed_at.isoformat() if item.completed_at else None,
                "completed_tz": item.completed_tz,
                "completed_by": str(item.completed_by) if item.completed_by else None,
                "completed_by_name": user_name_cache.get(item.completed_by) if item.completed_by else None,
                # review
                "review_result": item.review_result,
                "reviewer_id": str(item.reviewer_id) if item.reviewer_id else None,
                "reviewer_name": user_name_cache.get(item.reviewer_id) if item.reviewer_id else None,
                "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
            }

            # files — all files for this item, resolved URLs
            item_data["files"] = [
                {
                    "id": str(f.id),
                    "context": f.context,
                    "context_id": str(f.context_id) if f.context_id else None,
                    "file_url": _resolve(f.file_url),
                    "file_type": f.file_type,
                    "sort_order": f.sort_order,
                }
                for f in sorted(item.files or [], key=lambda f: f.created_at)
            ]

            # submissions — version history
            item_data["submissions"] = [
                {
                    "id": str(sub.id),
                    "version": sub.version,
                    "note": sub.note,
                    "location": sub.location,
                    "submitted_by": str(sub.submitted_by) if sub.submitted_by else None,
                    "submitted_by_name": user_name_cache.get(sub.submitted_by) if sub.submitted_by else None,
                    "submitted_at": sub.submitted_at.isoformat(),
                }
                for sub in sorted(item.submissions or [], key=lambda s: s.version)
            ]

            # reviews_log — review result changes
            item_data["reviews_log"] = [
                {
                    "id": str(log.id),
                    "old_result": log.old_result,
                    "new_result": log.new_result,
                    "comment": log.comment,
                    "changed_by": str(log.changed_by) if log.changed_by else None,
                    "changed_by_name": user_name_cache.get(log.changed_by) if log.changed_by else None,
                    "created_at": log.created_at.isoformat(),
                }
                for log in sorted(item.reviews_log or [], key=lambda l: l.created_at)
            ]

            # messages — chat thread
            item_data["messages"] = [
                {
                    "id": str(msg.id),
                    "author_id": str(msg.author_id) if msg.author_id else None,
                    "author_name": user_name_cache.get(msg.author_id) if msg.author_id else None,
                    "content": msg.content,
                    "created_at": msg.created_at.isoformat(),
                }
                for msg in sorted(item.messages or [], key=lambda m: m.created_at)
            ]

            items_list.append(item_data)

        response["items"] = items_list
        return response


# 싱글턴 인스턴스 — Singleton instance
checklist_instance_service: ChecklistInstanceService = ChecklistInstanceService()
