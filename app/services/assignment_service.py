"""업무 배정 서비스 — 업무 배정 비즈니스 로직.

Assignment Service — Business logic for work assignment management.
Handles assignment creation with JSONB snapshot, bulk creation, completion tracking,
and automatic notification creation.
"""

import copy
from datetime import date, datetime, timezone
from typing import Sequence
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.assignment import WorkAssignment
from app.models.checklist import ChecklistTemplate
from app.models.organization import Store
from app.models.user import User
from app.models.work import Position, Shift
from app.repositories.assignment_repository import assignment_repository
from app.repositories.checklist_repository import checklist_repository
from app.schemas.common import AssignmentCreate
from app.utils.exceptions import BadRequestError, DuplicateError, ForbiddenError, NotFoundError


class AssignmentService:
    """업무 배정 서비스.

    Work assignment service handling creation, snapshot generation,
    completion tracking, and notification dispatch.
    """

    async def _validate_store_ownership(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
    ) -> Store:
        """매장이 해당 조직에 속하는지 검증합니다.

        Verify that a store belongs to the specified organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 UUID (Store UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            Store: 검증된 매장 (Verified store)

        Raises:
            NotFoundError: 매장이 없을 때 (When store not found)
            ForbiddenError: 다른 조직 매장일 때 (When store belongs to another org)
        """
        result = await db.execute(select(Store).where(Store.id == store_id))
        store: Store | None = result.scalar_one_or_none()

        if store is None:
            raise NotFoundError("매장을 찾을 수 없습니다 (Store not found)")
        if store.organization_id != organization_id:
            raise ForbiddenError("해당 매장에 대한 권한이 없습니다 (No permission for this store)")
        return store

    async def _build_checklist_snapshot(
        self,
        db: AsyncSession,
        store_id: UUID,
        shift_id: UUID,
        position_id: UUID,
        work_date: date | None = None,
    ) -> tuple[dict | None, int]:
        """체크리스트 템플릿으로부터 JSONB 스냅샷을 생성합니다.

        Build a JSONB checklist snapshot from the matching template.
        Respects recurrence settings: daily templates always match,
        weekly templates only match if work_date's weekday is in recurrence_days.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            store_id: 매장 UUID (Store UUID)
            shift_id: 근무조 UUID (Shift UUID)
            position_id: 포지션 UUID (Position UUID)
            work_date: 근무일 (Work date for recurrence filtering)

        Returns:
            tuple[dict | None, int]: (스냅샷 딕셔너리 또는 None, 총 항목 수)
                                      (Snapshot dict or None, total item count)
        """
        # 해당 조합의 템플릿 검색 — Find matching template
        templates: Sequence[ChecklistTemplate] = await checklist_repository.get_by_store(
            db, store_id, shift_id, position_id
        )

        if not templates:
            return None, 0

        template: ChecklistTemplate = templates[0]

        # 항목별 반복 주기 필터 — Per-item recurrence filter
        weekday: int | None = work_date.weekday() if work_date else None  # Monday=0 ~ Sunday=6

        # 항목 스냅샷 생성 — Generate item snapshot (item별 recurrence 필터링)
        items_snapshot: list[dict] = []
        idx: int = 0
        for item in template.items:
            # item별 recurrence 체크 — skip if weekly and work_date not in item's recurrence_days
            if weekday is not None and item.recurrence_type == "weekly":
                if item.recurrence_days and weekday not in item.recurrence_days:
                    continue

            items_snapshot.append(
                {
                    "item_index": idx,
                    "template_item_id": str(item.id),
                    "title": item.title,
                    "description": item.description,
                    "verification_type": item.verification_type,
                    "sort_order": item.sort_order,
                    "is_completed": False,
                    "completed_at": None,
                    "completed_tz": None,
                }
            )
            idx += 1

        # 해당 날짜에 매칭되는 item이 없으면 None 반환
        if not items_snapshot:
            return None, 0

        snapshot: dict = {
            "template_id": str(template.id),
            "template_name": template.title,
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "items": items_snapshot,
        }

        return snapshot, len(items_snapshot)

    async def create_assignment(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: AssignmentCreate,
        assigned_by: UUID,
    ) -> WorkAssignment:
        """새 업무 배정을 생성하고 체크리스트 스냅샷을 첨부합니다.

        Create a new work assignment and attach a checklist snapshot.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)
            data: 배정 생성 데이터 (Assignment creation data)
            assigned_by: 배정자 UUID (Assigner's UUID)

        Returns:
            WorkAssignment: 생성된 업무 배정 (Created work assignment)

        Raises:
            NotFoundError: 매장이 없을 때 (When store not found)
            ForbiddenError: 다른 조직 매장일 때 (When store belongs to another org)
            DuplicateError: 같은 날짜에 중복 배정 시 (When duplicate assignment exists)
        """
        store_id: UUID = UUID(data.store_id)
        shift_id: UUID = UUID(data.shift_id)
        position_id: UUID = UUID(data.position_id)
        user_id: UUID = UUID(data.user_id)

        # 매장 소유권 검증 — Verify store ownership
        await self._validate_store_ownership(db, store_id, organization_id)

        # 중복 배정 검사 — Check for duplicate assignment
        is_duplicate: bool = await assignment_repository.check_duplicate(
            db, store_id, shift_id, position_id, user_id, data.work_date
        )
        if is_duplicate:
            raise DuplicateError(
                "해당 날짜에 동일한 배정이 이미 존재합니다 "
                "(An assignment for this combination on this date already exists)"
            )

        # 체크리스트 스냅샷 생성 (반복 주기 필터 포함) — Build checklist snapshot with recurrence filter
        snapshot: dict | None
        total_items: int
        snapshot, total_items = await self._build_checklist_snapshot(
            db, store_id, shift_id, position_id, work_date=data.work_date
        )

        # 체크리스트 템플릿 필수 검증 — Require checklist template
        if snapshot is None:
            raise BadRequestError(
                "해당 조합에 체크리스트 템플릿이 없습니다. 먼저 체크리스트를 생성해 주세요. "
                "(No checklist template exists for this combination. "
                "Please create a checklist template first.)"
            )

        assignment: WorkAssignment = await assignment_repository.create(
            db,
            {
                "organization_id": organization_id,
                "store_id": store_id,
                "shift_id": shift_id,
                "position_id": position_id,
                "user_id": user_id,
                "work_date": data.work_date,
                "status": "assigned",
                "checklist_snapshot": snapshot,
                "total_items": total_items,
                "completed_items": 0,
                "assigned_by": assigned_by,
            },
        )

        # 알림 자동 생성 — Auto-create notification
        from app.services.notification_service import notification_service

        await notification_service.create_for_assignment(db, assignment)

        return assignment

    async def bulk_create(
        self,
        db: AsyncSession,
        organization_id: UUID,
        assignments_data: list[AssignmentCreate],
        assigned_by: UUID,
    ) -> list[WorkAssignment]:
        """여러 업무 배정을 일괄 생성합니다.

        Bulk create multiple work assignments.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            assignments_data: 배정 생성 데이터 목록 (List of assignment creation data)
            assigned_by: 배정자 UUID (Assigner's UUID)

        Returns:
            list[WorkAssignment]: 생성된 배정 목록 (List of created assignments)
        """
        created: list[WorkAssignment] = []
        for data in assignments_data:
            assignment: WorkAssignment = await self.create_assignment(
                db, organization_id, data, assigned_by
            )
            created.append(assignment)
        return created

    async def build_response(
        self,
        db: AsyncSession,
        assignment: WorkAssignment,
    ) -> dict:
        """배정 응답 딕셔너리를 구성합니다 (관련 엔티티 이름 포함).

        Build assignment response dict with related entity names.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            assignment: 업무 배정 ORM 객체 (Work assignment ORM object)

        Returns:
            dict: 매장/근무조/포지션/사용자 이름이 포함된 응답 딕셔너리
                  (Response dict with store/shift/position/user names)
        """
        # 관련 엔티티 이름 조회 — Fetch related entity names
        store_result = await db.execute(select(Store.name).where(Store.id == assignment.store_id))
        store_name: str = store_result.scalar() or "Unknown"

        shift_result = await db.execute(select(Shift.name).where(Shift.id == assignment.shift_id))
        shift_name: str = shift_result.scalar() or "Unknown"

        position_result = await db.execute(
            select(Position.name).where(Position.id == assignment.position_id)
        )
        position_name: str = position_result.scalar() or "Unknown"

        user_result = await db.execute(
            select(User.full_name).where(User.id == assignment.user_id)
        )
        user_name: str = user_result.scalar() or "Unknown"

        return {
            "id": str(assignment.id),
            "store_id": str(assignment.store_id),
            "store_name": store_name,
            "shift_id": str(assignment.shift_id),
            "shift_name": shift_name,
            "position_id": str(assignment.position_id),
            "position_name": position_name,
            "user_id": str(assignment.user_id),
            "user_name": user_name,
            "work_date": assignment.work_date,
            "status": assignment.status,
            "total_items": assignment.total_items,
            "completed_items": assignment.completed_items,
            "created_at": assignment.created_at,
        }

    async def build_detail_response(
        self,
        db: AsyncSession,
        assignment: WorkAssignment,
    ) -> dict:
        """배정 상세 응답 딕셔너리를 구성합니다 (체크리스트 스냅샷 포함).

        Build assignment detail response dict with checklist snapshot.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            assignment: 업무 배정 ORM 객체 (Work assignment ORM object)

        Returns:
            dict: 체크리스트 스냅샷이 포함된 상세 응답 딕셔너리
                  (Detail response dict with checklist snapshot)
        """
        response: dict = await self.build_response(db, assignment)
        snapshot: dict | None = assignment.checklist_snapshot
        response["checklist_snapshot"] = snapshot.get("items") if snapshot else None
        return response

    async def list_assignments(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None = None,
        user_id: UUID | None = None,
        work_date: date | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[WorkAssignment], int]:
        """업무 배정 목록을 필터링하여 페이지네이션 조회합니다.

        List work assignments with filters and pagination.

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
            tuple[Sequence[WorkAssignment], int]: (배정 목록, 전체 개수)
                                                   (List of assignments, total count)
        """
        return await assignment_repository.get_by_filters(
            db, organization_id, store_id, user_id, work_date, status, page, per_page
        )

    async def get_detail(
        self,
        db: AsyncSession,
        assignment_id: UUID,
        organization_id: UUID,
    ) -> WorkAssignment:
        """업무 배정 상세 정보를 조회합니다.

        Get work assignment detail with checklist snapshot.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            assignment_id: 배정 UUID (Assignment UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            WorkAssignment: 배정 상세 (Assignment detail)

        Raises:
            NotFoundError: 배정이 없을 때 (When assignment not found)
        """
        assignment: WorkAssignment | None = await assignment_repository.get_detail(
            db, assignment_id, organization_id
        )
        if assignment is None:
            raise NotFoundError("업무 배정을 찾을 수 없습니다 (Work assignment not found)")
        return assignment

    async def delete_assignment(
        self,
        db: AsyncSession,
        assignment_id: UUID,
        organization_id: UUID,
    ) -> bool:
        """업무 배정을 삭제합니다.

        Delete a work assignment.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            assignment_id: 배정 UUID (Assignment UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            bool: 삭제 성공 여부 (Whether the deletion was successful)

        Raises:
            NotFoundError: 배정이 없을 때 (When assignment not found)
        """
        deleted: bool = await assignment_repository.delete(db, assignment_id, organization_id)
        if not deleted:
            raise NotFoundError("업무 배정을 찾을 수 없습니다 (Work assignment not found)")
        return deleted

    async def get_recent_users(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID,
        exclude_date: date | None = None,
        days: int = 30,
    ) -> list[dict]:
        """매장 내 최근 배정된 사용자 목록을 조회합니다.

        Get recently assigned users per shift x position combo for a store.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            store_id: 매장 UUID (Store UUID)
            exclude_date: 제외할 날짜 (Date to exclude, usually today)
            days: 조회 기간 일수 (Lookback period in days)

        Returns:
            list[dict]: 최근 배정 사용자 목록 (Recent assignment user list)
        """
        rows = await assignment_repository.get_recent_user_ids(
            db, organization_id, store_id, exclude_date, days
        )
        return [
            {
                "shift_id": str(row.shift_id),
                "position_id": str(row.position_id),
                "user_id": str(row.user_id),
                "last_work_date": row.last_work_date,
            }
            for row in rows
        ]

    async def get_my_assignments(
        self,
        db: AsyncSession,
        user_id: UUID,
        work_date: date | None = None,
        status: str | None = None,
    ) -> Sequence[WorkAssignment]:
        """내 업무 배정 목록을 조회합니다 (앱용).

        Get my work assignments for the app.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)
            work_date: 근무일 필터, 선택 (Optional work date filter)
            status: 상태 필터, 선택 (Optional status filter)

        Returns:
            Sequence[WorkAssignment]: 내 배정 목록 (My assignment list)
        """
        return await assignment_repository.get_user_assignments(db, user_id, work_date, status)

    async def complete_checklist_item(
        self,
        db: AsyncSession,
        assignment_id: UUID,
        user_id: UUID,
        item_index: int,
        is_completed: bool,
        client_timezone: str = "America/Los_Angeles",
    ) -> WorkAssignment:
        """체크리스트 항목을 완료/미완료 처리합니다.

        Complete or uncomplete a checklist item in the JSONB snapshot.
        Auto-updates assignment status when all items are completed.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            assignment_id: 배정 UUID (Assignment UUID)
            user_id: 사용자 UUID (User UUID)
            item_index: 체크리스트 항목 인덱스 (Checklist item index)
            is_completed: 완료 여부 (Whether item is completed)
            client_timezone: 클라이언트 IANA 타임존 (Client IANA timezone for display)

        Returns:
            WorkAssignment: 업데이트된 배정 (Updated assignment)

        Raises:
            NotFoundError: 배정이 없을 때 (When assignment not found)
            ForbiddenError: 다른 사용자의 배정일 때 (When assignment belongs to another user)
            BadRequestError: 스냅샷이 없거나 인덱스 범위 초과 시
                             (When snapshot is missing or index is out of range)
        """
        # 배정 조회 — Fetch assignment
        assignment: WorkAssignment | None = await assignment_repository.get_by_id(
            db, assignment_id
        )
        if assignment is None:
            raise NotFoundError("업무 배정을 찾을 수 없습니다 (Work assignment not found)")

        # 본인 확인 — Verify ownership
        if assignment.user_id != user_id:
            raise ForbiddenError("본인의 배정만 수정할 수 있습니다 (Can only modify your own assignment)")

        # 스냅샷 유효성 검증 — Validate snapshot (deepcopy로 JSONB 변경 감지 보장)
        # Use deepcopy to ensure SQLAlchemy detects the JSONB mutation
        snapshot: dict | None = copy.deepcopy(assignment.checklist_snapshot)
        if snapshot is None or "items" not in snapshot:
            raise BadRequestError("체크리스트 스냅샷이 없습니다 (No checklist snapshot)")

        items: list[dict] = snapshot["items"]
        if item_index < 0 or item_index >= len(items):
            raise BadRequestError(
                f"항목 인덱스가 범위를 벗어났습니다 (Item index out of range: {item_index})"
            )

        # 항목 업데이트 — Update item
        items[item_index]["is_completed"] = is_completed
        if is_completed:
            # 클라이언트 타임존 기준 시각/타임존 분리 저장 — Store local time (HH:MM) + tz abbreviation separately
            try:
                tz = ZoneInfo(client_timezone)
            except (KeyError, ValueError):
                tz = ZoneInfo("America/Los_Angeles")
            local_now = datetime.now(tz)
            items[item_index]["completed_at"] = local_now.strftime("%Y-%m-%dT%H:%M")  # "2026-02-20T14:05"
            items[item_index]["completed_tz"] = local_now.strftime("%Z")  # "PST", "KST"
        else:
            items[item_index]["completed_at"] = None
            items[item_index]["completed_tz"] = None

        # 완료 항목 수 재계산 — Recalculate completed count
        completed_count: int = sum(1 for item in items if item["is_completed"])

        # 상태 자동 업데이트 — Auto-update status
        new_status: str
        if completed_count == len(items):
            new_status = "completed"
        elif completed_count > 0:
            new_status = "in_progress"
        else:
            new_status = "assigned"

        # JSONB 필드 업데이트를 위해 직접 할당 — Direct assign for JSONB update
        assignment.checklist_snapshot = snapshot
        assignment.completed_items = completed_count
        assignment.status = new_status

        await db.flush()
        await db.refresh(assignment)
        return assignment


# 싱글턴 인스턴스 — Singleton instance
assignment_service: AssignmentService = AssignmentService()
