"""알림 레포지토리 — 알림 관련 DB 쿼리 담당.

Alert Repository — Handles all alert-related database queries.
Extends BaseRepository with user-specific alert operations.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.repositories.base import BaseRepository


class AlertRepository(BaseRepository[Alert]):
    """알림 레포지토리.

    Alert repository with user-specific read/unread operations.

    Extends:
        BaseRepository[Alert]
    """

    def __init__(self) -> None:
        """레포지토리를 초기화합니다.

        Initialize the alert repository with Alert model.
        """
        super().__init__(Alert)

    async def get_user_alerts(
        self,
        db: AsyncSession,
        user_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Alert], int]:
        """사용자의 알림 목록을 페이지네이션하여 조회합니다.

        Retrieve paginated alerts for a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)
            page: 페이지 번호, 1부터 시작 (Page number, 1-based)
            per_page: 페이지당 항목 수 (Items per page)

        Returns:
            tuple[Sequence[Alert], int]: (알림 목록, 전체 개수)
                                                 (List of alerts, total count)
        """
        query: Select = (
            select(Alert)
            .where(Alert.user_id == user_id)
            .order_by(Alert.created_at.desc())
        )
        return await self.get_paginated(db, query, page, per_page)

    async def get_unread_count(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> int:
        """사용자의 읽지 않은 알림 수를 조회합니다.

        Get the count of unread alerts for a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)

        Returns:
            int: 읽지 않은 알림 수 (Count of unread alerts)
        """
        query: Select = (
            select(func.count())
            .select_from(Alert)
            .where(
                Alert.user_id == user_id,
                Alert.is_read.is_(False),
            )
        )
        count: int = (await db.execute(query)).scalar() or 0
        return count

    async def mark_read(
        self,
        db: AsyncSession,
        alert_id: UUID,
        user_id: UUID,
    ) -> bool:
        """단일 알림을 읽음 처리합니다.

        Mark a single alert as read.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            alert_id: 알림 UUID (Alert UUID)
            user_id: 사용자 UUID (User UUID)

        Returns:
            bool: 처리 성공 여부 (Whether the operation was successful)
        """
        result = await db.execute(
            update(Alert)
            .where(
                Alert.id == alert_id,
                Alert.user_id == user_id,
            )
            .values(is_read=True)
        )
        await db.flush()
        return result.rowcount > 0

    async def mark_all_read(
        self,
        db: AsyncSession,
        user_id: UUID,
    ) -> int:
        """사용자의 모든 읽지 않은 알림을 읽음 처리합니다.

        Mark all unread alerts as read for a user.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            user_id: 사용자 UUID (User UUID)

        Returns:
            int: 업데이트된 알림 수 (Count of updated alerts)
        """
        result = await db.execute(
            update(Alert)
            .where(
                Alert.user_id == user_id,
                Alert.is_read.is_(False),
            )
            .values(is_read=True)
        )
        await db.flush()
        return result.rowcount

    async def create_alert(
        self,
        db: AsyncSession,
        organization_id: UUID,
        user_id: UUID,
        alert_type: str,
        message: str,
        reference_type: str | None = None,
        reference_id: UUID | None = None,
    ) -> Alert:
        """새 알림을 생성합니다.

        Create a new alert.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 UUID (Organization UUID)
            user_id: 수신자 UUID (Recipient user UUID)
            alert_type: 알림 유형 (Alert type)
            message: 알림 메시지 (Alert message)
            reference_type: 참조 유형, 선택 (Optional reference type)
            reference_id: 참조 ID, 선택 (Optional reference UUID)

        Returns:
            Alert: 생성된 알림 (Created alert)
        """
        alert: Alert = Alert(
            organization_id=organization_id,
            user_id=user_id,
            type=alert_type,
            message=message,
            reference_type=reference_type,
            reference_id=reference_id,
        )
        db.add(alert)
        await db.flush()
        await db.refresh(alert)
        return alert


# 싱글턴 인스턴스 — Singleton instance
alert_repository: AlertRepository = AlertRepository()
