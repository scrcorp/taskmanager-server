"""근무가능시간 레포지토리 — DB 쿼리 전용. commit 하지 않는다(서비스가 트랜잭션 소유)."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.availability import (
    StaffAvailability,
    StaffAvailabilityHistory,
    StaffAvailabilityPreset,
)
from app.models.user_store import UserStore
from app.repositories.base import BaseRepository


class AvailabilityRepository(BaseRepository[StaffAvailability]):
    def __init__(self) -> None:
        super().__init__(StaffAvailability)

    async def list_for_user(
        self, db: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[StaffAvailability]:
        q = select(StaffAvailability).where(
            StaffAvailability.organization_id == organization_id,
            StaffAvailability.user_id == user_id,
        )
        return list((await db.execute(q)).scalars().all())

    async def list_for_users(
        self, db: AsyncSession, organization_id: uuid.UUID, user_ids: list[uuid.UUID]
    ) -> list[StaffAvailability]:
        if not user_ids:
            return []
        q = select(StaffAvailability).where(
            StaffAvailability.organization_id == organization_id,
            StaffAvailability.user_id.in_(user_ids),
        )
        return list((await db.execute(q)).scalars().all())

    async def get_day(
        self,
        db: AsyncSession,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        day_of_week: int,
    ) -> StaffAvailability | None:
        q = select(StaffAvailability).where(
            StaffAvailability.organization_id == organization_id,
            StaffAvailability.user_id == user_id,
            StaffAvailability.day_of_week == day_of_week,
        )
        return (await db.execute(q)).scalar_one_or_none()

    async def delete_row(self, db: AsyncSession, row: StaffAvailability) -> None:
        await db.delete(row)
        await db.flush()

    async def user_ids_in_store(
        self, db: AsyncSession, store_id: uuid.UUID
    ) -> list[uuid.UUID]:
        q = select(UserStore.user_id).where(UserStore.store_id == store_id)
        return list((await db.execute(q)).scalars().all())

    async def user_ids_in_stores(
        self, db: AsyncSession, store_ids: list[uuid.UUID]
    ) -> list[uuid.UUID]:
        """여러 매장의 user_id 를 한 번의 쿼리로 (DISTINCT). N+1 방지."""
        if not store_ids:
            return []
        q = (
            select(UserStore.user_id)
            .where(UserStore.store_id.in_(store_ids))
            .distinct()
        )
        return list((await db.execute(q)).scalars().all())


availability_repository = AvailabilityRepository()


class AvailabilityHistoryRepository:
    """append-only. update/delete 하지 않는다."""

    async def append(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        day_of_week: int | None,
        actor_id: uuid.UUID | None,
        source: str,
        snapshot: dict,
        prev: dict | None = None,
        description: str | None = None,
        created_at: datetime | None = None,
    ) -> StaffAvailabilityHistory:
        row = StaffAvailabilityHistory(
            user_id=user_id,
            organization_id=organization_id,
            day_of_week=day_of_week,
            actor_id=actor_id,
            source=source,
            snapshot=snapshot,
            prev=prev,
            description=description,
            # 한 번의 save 에서 온 행들은 같은 타임스탬프 → 콘솔이 "save 그룹"으로 묶음
            created_at=created_at or datetime.now(timezone.utc),
        )
        db.add(row)
        await db.flush()
        return row

    async def exists_for_user(
        self, db: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        """(user, org) 에 이력 행이 한 건이라도 존재하는지. append-only 라 삭제되지 않음."""
        q = (
            select(StaffAvailabilityHistory.id)
            .where(
                StaffAvailabilityHistory.organization_id == organization_id,
                StaffAvailabilityHistory.user_id == user_id,
            )
            .limit(1)
        )
        return (await db.execute(q)).first() is not None

    async def list_for_user(
        self,
        db: AsyncSession,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        limit: int = 50,
    ) -> list[StaffAvailabilityHistory]:
        q = (
            select(StaffAvailabilityHistory)
            .where(
                StaffAvailabilityHistory.organization_id == organization_id,
                StaffAvailabilityHistory.user_id == user_id,
            )
            .order_by(StaffAvailabilityHistory.created_at.desc())
            .limit(limit)
        )
        return list((await db.execute(q)).scalars().all())


availability_history_repository = AvailabilityHistoryRepository()


class AvailabilityPresetRepository(BaseRepository[StaffAvailabilityPreset]):
    """org custom 프리셋 CRUD. commit 하지 않는다(서비스가 트랜잭션 소유)."""

    def __init__(self) -> None:
        super().__init__(StaffAvailabilityPreset)

    async def list_for_org(
        self, db: AsyncSession, organization_id: uuid.UUID
    ) -> list[StaffAvailabilityPreset]:
        q = (
            select(StaffAvailabilityPreset)
            .where(StaffAvailabilityPreset.organization_id == organization_id)
            .order_by(StaffAvailabilityPreset.created_at.asc())
        )
        return list((await db.execute(q)).scalars().all())

    async def get_owned(
        self, db: AsyncSession, organization_id: uuid.UUID, preset_id: uuid.UUID
    ) -> StaffAvailabilityPreset | None:
        q = select(StaffAvailabilityPreset).where(
            StaffAvailabilityPreset.organization_id == organization_id,
            StaffAvailabilityPreset.id == preset_id,
        )
        return (await db.execute(q)).scalar_one_or_none()

    async def get_by_name(
        self, db: AsyncSession, organization_id: uuid.UUID, name: str
    ) -> StaffAvailabilityPreset | None:
        q = select(StaffAvailabilityPreset).where(
            StaffAvailabilityPreset.organization_id == organization_id,
            StaffAvailabilityPreset.name == name,
        )
        return (await db.execute(q)).scalar_one_or_none()

    async def delete_row(self, db: AsyncSession, row: StaffAvailabilityPreset) -> None:
        await db.delete(row)
        await db.flush()


availability_preset_repository = AvailabilityPresetRepository()
