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

from app.models.assignment import WorkAssignment
from app.models.checklist import ChecklistCompletion, ChecklistInstance, ChecklistTemplate
from app.models.organization import Store
from app.models.user import User
from app.repositories.checklist_instance_repository import checklist_instance_repository
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

            merged_items: list[dict] = []
            for item in snapshot["items"]:
                item_data: dict[str, Any] = {**item}
                comp: ChecklistCompletion | None = completions_map.get(item["item_index"])
                if comp is not None:
                    item_data["is_completed"] = True
                    item_data["completed_at"] = comp.completed_at.isoformat() if comp.completed_at else None
                    item_data["completed_timezone"] = comp.completed_timezone
                    item_data["completed_by"] = str(comp.user_id)
                    item_data["photo_url"] = comp.photo_url
                    item_data["note"] = comp.note
                    item_data["location"] = comp.location
                else:
                    item_data["is_completed"] = False
                    item_data["completed_at"] = None
                    item_data["completed_timezone"] = None
                    item_data["completed_by"] = None
                    item_data["photo_url"] = None
                    item_data["note"] = None
                    item_data["location"] = None

                merged_items.append(item_data)

            response["snapshot"] = merged_items
        else:
            response["snapshot"] = None

        return response


# 싱글턴 인스턴스 — Singleton instance
checklist_instance_service: ChecklistInstanceService = ChecklistInstanceService()
