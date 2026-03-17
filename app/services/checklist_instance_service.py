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
)
from app.models.organization import Store
from app.models.user import User
from app.repositories.checklist_instance_repository import checklist_instance_repository
from app.services.storage_service import storage_service
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
        note: str | None = None,
        location: dict | None = None,
        client_timezone: str = "America/Los_Angeles",
    ) -> ChecklistInstance:
        """체크리스트 항목을 완료 처리합니다.

        Updates cl_instance_items row and creates cl_item_files + cl_item_submissions.

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
                f"항목 인덱스가 범위를 벗어났습니다 (Item index out of range: {item_index})"
            )

        if target_item.is_completed:
            raise BadRequestError(
                f"이미 완료된 항목입니다 (Item {item_index} is already completed)"
            )

        # 항목 타입별 검증
        v_type: str = target_item.verification_type or "none"
        if "photo" in v_type and not photo_url:
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

            # 파일 저장
            if photo_url:
                finalized = storage_service.finalize_upload(photo_url)
                file_row = ChecklistItemFile(
                    item_id=target_item.id,
                    context="submission",
                    context_id=submission.id,
                    file_url=finalized,
                    file_type="photo",
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
        if target_item is None or not target_item.is_completed:
            raise BadRequestError("Cannot resubmit an uncompleted item")

        try:
            # 기존 submission 개수로 새 version 계산
            existing_version_count = len(target_item.submissions) if target_item.submissions else 0
            new_version = existing_version_count + 1

            # 새 제출 이력 생성
            now = datetime.now(timezone.utc)
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

            # 새 파일 추가
            if photo_url is not None:
                new_url = storage_service.finalize_upload(photo_url)
                if new_url:
                    new_file = ChecklistItemFile(
                        item_id=target_item.id,
                        context="submission",
                        context_id=new_submission.id,
                        file_url=new_url,
                        file_type="photo",
                        uploaded_by=user_id,
                    )
                    db.add(new_file)

            await db.flush()

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

        if content_type in ("photo", "video"):
            content = storage_service.finalize_upload(content)

        try:
            msg = ChecklistItemMessage(
                item_id=target_item.id,
                author_id=author_id,
                type=content_type,
                content=content,
            )
            db.add(msg)
            await db.flush()
            await db.refresh(msg)

            # compatibility shim: attach review_id alias onto msg for router response
            msg.review_id = target_item.id  # type: ignore[attr-defined]
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
            if existing.type in ("photo", "video"):
                storage_service.delete_file(existing.content)

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
            .options(selectinload(ChecklistInstanceItem.instance))
            .order_by(ChecklistInstanceItem.completed_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        result = await db.execute(data_query)
        items_orm: list[ChecklistInstanceItem] = list(result.scalars().all())

        items: list[dict] = []
        for item in items_orm:
            inst: ChecklistInstance = item.instance

            user_result = await db.execute(select(User.full_name).where(User.id == item.completed_by))
            user_name: str = user_result.scalar() or "Unknown"

            store_result = await db.execute(select(Store.name).where(Store.id == inst.store_id))
            store_name: str = store_result.scalar() or "Unknown"

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
                "note": item.note,
            })

        return items, total

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
            "created_at": instance.created_at,
        }

    async def build_detail_response(
        self,
        db: AsyncSession,
        instance: ChecklistInstance,
    ) -> dict:
        """인스턴스 상세 응답 딕셔너리를 구성합니다 (cl_instance_items 기반).

        Replaces the old JSONB snapshot+completions merge with a direct
        read from cl_instance_items.
        """
        response: dict = await self.build_response(db, instance)

        if not instance.items:
            response["snapshot"] = None
            return response

        # 이름 캐시 구성 — Collect all user IDs to fetch in bulk
        user_name_cache: dict[UUID, str] = {}
        user_ids_to_fetch: set[UUID] = set()
        for item in instance.items:
            if item.completed_by:
                user_ids_to_fetch.add(item.completed_by)
            if item.reviewer_id:
                user_ids_to_fetch.add(item.reviewer_id)
            for msg in (item.messages or []):
                if msg.author_id:
                    user_ids_to_fetch.add(msg.author_id)
            for log in (item.reviews_log or []):
                if log.changed_by:
                    user_ids_to_fetch.add(log.changed_by)

        for uid in user_ids_to_fetch:
            r = await db.execute(select(User.full_name).where(User.id == uid))
            user_name_cache[uid] = r.scalar() or "Unknown"

        merged_items: list[dict] = []
        for item in instance.items:
            # 현재 파일들 (submission_id is None = 현재 제출)
            current_files = [f for f in (item.files or []) if f.submission_id is None]
            # 아카이빙된 파일 포함 최신 파일도 포함
            all_files_sorted = sorted(item.files or [], key=lambda f: f.created_at)
            current_photo_url: str | None = None
            if current_files:
                current_photo_url = _resolve(sorted(current_files, key=lambda f: f.sort_order)[0].file_url)

            item_data: dict[str, Any] = {
                "item_index": item.item_index,
                "title": item.title,
                "description": item.description,
                "verification_type": item.verification_type,
                "sort_order": item.sort_order,
                "is_completed": item.is_completed,
                "completed_at": item.completed_at.isoformat() if item.completed_at else None,
                "completed_timezone": item.completed_tz,
                "completed_tz": item.completed_tz,
                "completed_by": str(item.completed_by) if item.completed_by else None,
                "completed_by_name": user_name_cache.get(item.completed_by) if item.completed_by else None,
                "photo_url": current_photo_url,
                "note": item.note,
                "location": item.location,
                "resubmission_count": max(len(item.submissions) - 1, 0) if item.submissions else 0,
            }

            # 리뷰 데이터 — from inline fields on item
            review_result = item.review_result
            if review_result is not None:
                reviewer_name = user_name_cache.get(item.reviewer_id) if item.reviewer_id else None

                # 메시지 목록 (review contents 대체)
                contents_list: list[dict] = []
                for msg in (item.messages or []):
                    # message files (context='chat')
                    msg_files = [f for f in (item.files or []) if f.context == "chat" and f.context_id == msg.id]
                    contents_list.append({
                        "id": str(msg.id),
                        "author_id": str(msg.author_id) if msg.author_id else None,
                        "author_name": user_name_cache.get(msg.author_id) if msg.author_id else None,
                        "content": msg.content,
                        "files": [{"id": str(f.id), "file_url": _resolve(f.file_url), "file_type": f.file_type} for f in msg_files],
                        "created_at": msg.created_at.isoformat(),
                    })

                # 리뷰 이력 목록
                history_list: list[dict] = []
                for log in (item.reviews_log or []):
                    history_list.append({
                        "id": str(log.id),
                        "changed_by": str(log.changed_by),
                        "changed_by_name": user_name_cache.get(log.changed_by) if log.changed_by else None,
                        "old_result": log.old_result,
                        "new_result": log.new_result,
                        "created_at": log.created_at.isoformat(),
                    })

                item_data["review"] = {
                    "id": str(item.id),
                    "reviewer_id": str(item.reviewer_id),
                    "reviewer_name": reviewer_name,
                    "result": review_result,
                    "contents": contents_list,
                    "history": history_list,
                    "created_at": item.created_at.isoformat(),
                    "updated_at": item.updated_at.isoformat(),
                }
                item_data["review_status"] = review_result

                # 반려 플랫 필드
                is_rejected = review_result == "fail"
                item_data["is_rejected"] = is_rejected
                if is_rejected:
                    # 마지막 fail 로그에서 코멘트/사진 추출
                    fail_logs = [h for h in (item.reviews_log or []) if h.new_result == "fail"]
                    if fail_logs:
                        last_fail = sorted(fail_logs, key=lambda h: h.created_at)[-1]
                        fail_time = last_fail.created_at
                        fail_msgs = [m for m in (item.messages or []) if m.author_id == item.reviewer_id and m.created_at >= fail_time]
                        rej_comment = next((m.content for m in fail_msgs if m.type == "text"), None)
                        rej_photos = [_resolve(m.content) for m in fail_msgs if m.type in ("photo", "video")]
                        item_data["rejection_comment"] = rej_comment
                        item_data["rejection_photo_urls"] = rej_photos
                        item_data["rejected_by"] = user_name_cache.get(last_fail.changed_by) if last_fail.changed_by else reviewer_name
                        item_data["rejected_at"] = last_fail.created_at.isoformat()
                    else:
                        item_data["rejection_comment"] = None
                        item_data["rejection_photo_urls"] = []
                        item_data["rejected_by"] = reviewer_name
                        item_data["rejected_at"] = item.reviewed_at.isoformat() if item.reviewed_at else None
                else:
                    item_data["rejection_comment"] = None
                    item_data["rejection_photo_urls"] = []
                    item_data["rejected_by"] = None
                    item_data["rejected_at"] = None

                # 승인 플랫 필드
                is_approved = review_result == "pass"
                item_data["is_approved"] = is_approved
                if is_approved:
                    pass_logs = [h for h in (item.reviews_log or []) if h.new_result == "pass"]
                    if pass_logs:
                        last_pass = sorted(pass_logs, key=lambda h: h.created_at)[-1]
                        pass_time = last_pass.created_at
                        pass_msgs = [m for m in (item.messages or []) if m.author_id == item.reviewer_id and m.created_at >= pass_time]
                        app_comment = next((m.content for m in pass_msgs if m.type == "text"), None)
                        app_photos = [_resolve(m.content) for m in pass_msgs if m.type in ("photo", "video")]
                        item_data["approval_comment"] = app_comment
                        item_data["approval_photo_urls"] = app_photos
                        item_data["approved_by"] = user_name_cache.get(last_pass.changed_by) if last_pass.changed_by else reviewer_name
                        item_data["approved_at"] = last_pass.created_at.isoformat()
                    else:
                        item_data["approval_comment"] = None
                        item_data["approval_photo_urls"] = []
                        item_data["approved_by"] = reviewer_name
                        item_data["approved_at"] = item.reviewed_at.isoformat() if item.reviewed_at else None
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

            # 제출 이력 (재제출 아카이브)
            completion_history_list: list[dict] = []
            for sub in (item.submissions or []):
                sub_photos = [_resolve(f.file_url) for f in (sub.files or [])]
                completion_history_list.append({
                    "id": str(sub.id),
                    "photo_url": sub_photos[0] if sub_photos else None,
                    "photo_urls": sub_photos,
                    "note": sub.note,
                    "location": sub.location,
                    "submitted_at": sub.submitted_at.isoformat(),
                    "created_at": sub.created_at.isoformat(),
                })
            item_data["completion_history"] = completion_history_list

            # 앱용 재제출 응답 필드
            is_currently_rejected = item_data.get("is_rejected", False)
            sub_count = len(item.submissions) if item.submissions else 0
            if not is_currently_rejected and sub_count > 1:
                latest_sub = sorted(item.submissions, key=lambda s: s.version)[-1] if item.submissions else None
                item_data["response_comment"] = latest_sub.note if latest_sub else None
                item_data["responded_by"] = user_name_cache.get(item.completed_by) if item.completed_by else None
                item_data["responded_at"] = (
                    completion_history_list[-1]["created_at"] if completion_history_list
                    else (item.completed_at.isoformat() if item.completed_at else None)
                )
            else:
                item_data["response_comment"] = None
                item_data["responded_at"] = None
                item_data["responded_by"] = None

            # 타임라인 이벤트 구성 (시간순)
            timeline_events: list[dict] = []

            # 1) 최초 완료 이벤트
            if item.is_completed:
                if item.submissions:
                    first_sub = sorted(item.submissions, key=lambda s: s.submitted_at)[0]
                    sub_photos = [_resolve(f.file_url) for f in (first_sub.files or [])]
                    timeline_events.append({
                        "type": "completed",
                        "comment": first_sub.note,
                        "photo_urls": sub_photos,
                        "by": user_name_cache.get(item.completed_by) if item.completed_by else None,
                        "at": first_sub.submitted_at.isoformat(),
                    })
                else:
                    timeline_events.append({
                        "type": "completed",
                        "comment": item.note,
                        "photo_urls": [current_photo_url] if current_photo_url else [],
                        "by": user_name_cache.get(item.completed_by) if item.completed_by else None,
                        "at": item.completed_at.isoformat() if item.completed_at else None,
                    })

            # 2) 리뷰 이력 이벤트
            sorted_logs = sorted(item.reviews_log or [], key=lambda h: h.created_at)
            sorted_msgs = sorted(item.messages or [], key=lambda m: m.created_at)
            for idx, log in enumerate(sorted_logs):
                log_time = log.created_at
                next_time = sorted_logs[idx + 1].created_at if idx + 1 < len(sorted_logs) else None
                rh_comment = None
                rh_photos: list[str] = []
                for m in sorted_msgs:
                    if m.created_at < log_time:
                        continue
                    if next_time is not None and m.created_at >= next_time:
                        break
                    if m.author_id == item.reviewer_id:
                        if m.type == "text":
                            rh_comment = m.content
                        elif m.type in ("photo", "video"):
                            rh_photos.append(_resolve(m.content))

                if log.new_result == "fail":
                    timeline_events.append({
                        "type": "rejected",
                        "comment": rh_comment,
                        "photo_urls": rh_photos,
                        "by": user_name_cache.get(log.changed_by) if log.changed_by else None,
                        "at": log.created_at.isoformat(),
                    })
                elif log.new_result == "pass":
                    timeline_events.append({
                        "type": "approved",
                        "comment": rh_comment,
                        "photo_urls": rh_photos,
                        "by": user_name_cache.get(log.changed_by) if log.changed_by else None,
                        "at": log.created_at.isoformat(),
                    })
                elif log.new_result == "pending_re_review":
                    timeline_events.append({
                        "type": "pending",
                        "comment": None,
                        "photo_urls": [],
                        "by": user_name_cache.get(log.changed_by) if log.changed_by else None,
                        "at": log.created_at.isoformat(),
                    })

            # 3) 재제출 이벤트 (submission 아카이브 순서)
            sorted_subs = sorted(item.submissions or [], key=lambda s: s.submitted_at)
            for i, sub in enumerate(sorted_subs):
                if i + 1 < len(sorted_subs):
                    next_sub = sorted_subs[i + 1]
                    next_photos = [_resolve(f.file_url) for f in (next_sub.files or [])]
                    resp_note = next_sub.note
                    resp_photos = next_photos
                else:
                    resp_note = item.note
                    resp_photos = [_resolve(f.file_url) for f in (current_files or [])]
                timeline_events.append({
                    "type": "responded",
                    "comment": resp_note,
                    "photo_urls": resp_photos,
                    "by": user_name_cache.get(item.completed_by) if item.completed_by else None,
                    "at": sub.created_at.isoformat(),
                })

            timeline_events.sort(key=lambda e: e.get("at") or "")
            item_data["history"] = timeline_events

            merged_items.append(item_data)

        response["snapshot"] = merged_items
        return response


# 싱글턴 인스턴스 — Singleton instance
checklist_instance_service: ChecklistInstanceService = ChecklistInstanceService()
