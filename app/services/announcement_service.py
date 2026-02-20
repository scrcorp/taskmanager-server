"""공지사항 서비스 — 공지사항 비즈니스 로직.

Announcement Service — Business logic for announcement management.
Handles admin CRUD and app-facing user-scoped queries with notification dispatch.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import Announcement
from app.models.organization import Store
from app.models.user import User
from app.models.user_store import UserStore
from app.repositories.announcement_repository import announcement_repository
from app.schemas.common import AnnouncementCreate, AnnouncementUpdate
from app.utils.exceptions import ForbiddenError, NotFoundError


class AnnouncementService:
    """공지사항 서비스.

    Announcement service providing admin CRUD and app-facing read operations.
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

    async def build_response(
        self,
        db: AsyncSession,
        announcement: Announcement,
    ) -> dict:
        """공지사항 응답 딕셔너리를 구성합니다 (관련 엔티티 이름 포함).

        Build announcement response dict with related entity names.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            announcement: 공지사항 ORM 객체 (Announcement ORM object)

        Returns:
            dict: 매장명/작성자명이 포함된 응답 딕셔너리
                  (Response dict with store name and creator name)
        """
        # 매장 이름 조회 — Fetch store name
        store_name: str | None = None
        if announcement.store_id is not None:
            result = await db.execute(
                select(Store.name).where(Store.id == announcement.store_id)
            )
            store_name = result.scalar()

        # 작성자 이름 조회 — Fetch creator name
        creator_result = await db.execute(
            select(User.full_name).where(User.id == announcement.created_by)
        )
        created_by_name: str = creator_result.scalar() or "Unknown"

        return {
            "id": str(announcement.id),
            "title": announcement.title,
            "content": announcement.content,
            "store_id": str(announcement.store_id) if announcement.store_id else None,
            "store_name": store_name,
            "created_by_name": created_by_name,
            "created_at": announcement.created_at,
        }

    # --- Admin CRUD ---

    async def list_announcements(
        self,
        db: AsyncSession,
        organization_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Announcement], int]:
        """조직의 공지사항 목록을 페이지네이션하여 조회합니다.

        List paginated announcements for an organization (admin).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            page: 페이지 번호 (Page number)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[Announcement], int]: (공지 목록, 전체 개수)
                                                 (List of announcements, total count)
        """
        return await announcement_repository.get_by_org(db, organization_id, page, per_page)

    async def get_detail(
        self,
        db: AsyncSession,
        announcement_id: UUID,
        organization_id: UUID,
    ) -> Announcement:
        """공지사항 상세를 조회합니다.

        Get announcement detail.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            announcement_id: 공지 UUID (Announcement UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            Announcement: 공지 상세 (Announcement detail)

        Raises:
            NotFoundError: 공지가 없을 때 (When announcement not found)
        """
        announcement: Announcement | None = await announcement_repository.get_by_id(
            db, announcement_id, organization_id
        )
        if announcement is None:
            raise NotFoundError("공지사항을 찾을 수 없습니다 (Announcement not found)")
        return announcement

    async def create_announcement(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: AnnouncementCreate,
        created_by: UUID,
    ) -> Announcement:
        """새 공지사항을 생성합니다.

        Create a new announcement and dispatch notifications.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            data: 공지 생성 데이터 (Announcement creation data)
            created_by: 작성자 UUID (Creator's UUID)

        Returns:
            Announcement: 생성된 공지 (Created announcement)

        Raises:
            NotFoundError: 매장이 없을 때 (When store not found)
            ForbiddenError: 다른 조직 매장일 때 (When store belongs to another org)
        """
        store_id: UUID | None = UUID(data.store_id) if data.store_id else None

        if store_id is not None:
            await self._validate_store_ownership(db, store_id, organization_id)

        announcement: Announcement = await announcement_repository.create(
            db,
            {
                "organization_id": organization_id,
                "store_id": store_id,
                "title": data.title,
                "content": data.content,
                "created_by": created_by,
            },
        )

        # 알림 자동 생성 — Auto-create notifications for affected users
        from app.services.notification_service import notification_service

        # 대상 사용자 조회 — Find target users
        user_ids: list[UUID] = await self._get_target_user_ids(
            db, organization_id, store_id
        )
        await notification_service.create_for_announcement(db, announcement, user_ids)

        return announcement

    async def update_announcement(
        self,
        db: AsyncSession,
        announcement_id: UUID,
        organization_id: UUID,
        data: AnnouncementUpdate,
    ) -> Announcement:
        """공지사항을 업데이트합니다.

        Update an announcement.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            announcement_id: 공지 UUID (Announcement UUID)
            organization_id: 조직 UUID (Organization UUID)
            data: 업데이트 데이터 (Update data)

        Returns:
            Announcement: 업데이트된 공지 (Updated announcement)

        Raises:
            NotFoundError: 공지가 없을 때 (When announcement not found)
        """
        update_data: dict = data.model_dump(exclude_unset=True)
        updated: Announcement | None = await announcement_repository.update(
            db, announcement_id, update_data, organization_id
        )
        if updated is None:
            raise NotFoundError("공지사항을 찾을 수 없습니다 (Announcement not found)")
        return updated

    async def delete_announcement(
        self,
        db: AsyncSession,
        announcement_id: UUID,
        organization_id: UUID,
    ) -> bool:
        """공지사항을 삭제합니다.

        Delete an announcement.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            announcement_id: 공지 UUID (Announcement UUID)
            organization_id: 조직 UUID (Organization UUID)

        Returns:
            bool: 삭제 성공 여부 (Whether the deletion was successful)

        Raises:
            NotFoundError: 공지가 없을 때 (When announcement not found)
        """
        deleted: bool = await announcement_repository.delete(
            db, announcement_id, organization_id
        )
        if not deleted:
            raise NotFoundError("공지사항을 찾을 수 없습니다 (Announcement not found)")
        return deleted

    # --- App (사용자용 조회) ---

    async def list_for_user(
        self,
        db: AsyncSession,
        organization_id: UUID,
        user_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Announcement], int]:
        """사용자가 볼 수 있는 공지사항 목록을 조회합니다.

        List announcements visible to the user (org-wide + user's stores).

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            user_id: 사용자 UUID (User UUID)
            page: 페이지 번호 (Page number)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[Announcement], int]: (공지 목록, 전체 개수)
                                                 (List of announcements, total count)
        """
        # 사용자의 매장 ID 목록 조회 — Get user's store IDs
        store_ids: list[UUID] = await self._get_user_store_ids(db, user_id)
        return await announcement_repository.get_for_user_stores(
            db, organization_id, store_ids, page, per_page
        )

    async def _get_user_store_ids(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> list[UUID]:
        """사용자가 속한 매장 ID 목록을 조회합니다.

        Get list of store UUIDs the user belongs to.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)

        Returns:
            list[UUID]: 매장 UUID 목록 (List of store UUIDs)
        """
        result = await db.execute(
            select(UserStore.store_id).where(UserStore.user_id == user_id)
        )
        return list(result.scalars().all())

    async def _get_target_user_ids(
        self,
        db: AsyncSession,
        organization_id: UUID,
        store_id: UUID | None,
    ) -> list[UUID]:
        """알림 대상 사용자 ID 목록을 조회합니다.

        Get list of user UUIDs to notify for an announcement.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            store_id: 매장 UUID (None이면 조직 전체)
                      (Store UUID, None means org-wide)

        Returns:
            list[UUID]: 대상 사용자 UUID 목록 (Target user UUID list)
        """
        if store_id is None:
            # 조직 전체 사용자 — All users in the organization
            result = await db.execute(
                select(User.id).where(
                    User.organization_id == organization_id,
                    User.is_active.is_(True),
                )
            )
        else:
            # 해당 매장 소속 사용자 — Users belonging to the specific store
            result = await db.execute(
                select(UserStore.user_id).where(UserStore.store_id == store_id)
            )
        return list(result.scalars().all())


# 싱글턴 인스턴스 — Singleton instance
announcement_service: AnnouncementService = AnnouncementService()
