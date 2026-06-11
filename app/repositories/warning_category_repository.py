"""경고 사유 카테고리 Repository — 순수 DB 쿼리 (org-scope).

라벨 live 조회 / 검증 / 시드 / revive 를 위한 조회만. 비즈니스 로직 없음.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.warning_category import WarningCategory


class WarningCategoryRepository:
    """경고 카테고리 DB 쿼리 (org-scope)."""

    async def list_for_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
        *,
        include_hidden: bool = True,
        include_deleted: bool = False,
    ) -> list[WarningCategory]:
        """org 카테고리 목록. sort_order 순(system=other 은 맨 끝).

        관리화면: include_hidden=True (Hidden 도 보여줌), include_deleted=False.
        picker: include_hidden=False (활성·표시만).
        """
        stmt = select(WarningCategory).where(
            WarningCategory.organization_id == organization_id
        )
        if not include_deleted:
            stmt = stmt.where(WarningCategory.deleted_at.is_(None))
        if not include_hidden:
            stmt = stmt.where(WarningCategory.is_hidden.is_(False))
        stmt = stmt.order_by(WarningCategory.sort_order, WarningCategory.created_at)
        return list((await db.execute(stmt)).scalars().all())

    async def get_by_code(
        self, db: AsyncSession, organization_id: UUID, code: str
    ) -> WarningCategory | None:
        """code 로 단건 (deleted 포함 — revive/유일성 판정용)."""
        stmt = select(WarningCategory).where(
            WarningCategory.organization_id == organization_id,
            WarningCategory.code == code,
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    async def get_by_id(
        self, db: AsyncSession, organization_id: UUID, category_id: UUID
    ) -> WarningCategory | None:
        """id 로 단건 (비삭제만)."""
        stmt = select(WarningCategory).where(
            WarningCategory.organization_id == organization_id,
            WarningCategory.id == category_id,
            WarningCategory.deleted_at.is_(None),
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    async def non_deleted_codes(
        self, db: AsyncSession, organization_id: UUID
    ) -> set[str]:
        """비삭제 코드 집합 (검증용 — hidden 도 포함, system 포함)."""
        stmt = select(WarningCategory.code).where(
            WarningCategory.organization_id == organization_id,
            WarningCategory.deleted_at.is_(None),
        )
        return set((await db.execute(stmt)).scalars().all())

    async def labels_by_code(
        self, db: AsyncSession, organization_id: UUID
    ) -> dict[str, str]:
        """code → label 맵 (deleted 포함 — 과거 경고의 legacy 코드도 라벨 resolve)."""
        stmt = select(WarningCategory.code, WarningCategory.label).where(
            WarningCategory.organization_id == organization_id
        )
        return {code: label for code, label in (await db.execute(stmt)).all()}

    async def max_sort_order(
        self, db: AsyncSession, organization_id: UUID
    ) -> int:
        """비시스템 카테고리의 최대 sort_order (새 카테고리 append 위치)."""
        stmt = select(func.max(WarningCategory.sort_order)).where(
            WarningCategory.organization_id == organization_id,
            WarningCategory.is_system.is_(False),
        )
        return (await db.execute(stmt)).scalar() or 0

    async def count_for_org(
        self, db: AsyncSession, organization_id: UUID
    ) -> int:
        """org 카테고리 행 수 (deleted 포함 — 시드 idempotency 판정)."""
        stmt = select(func.count()).select_from(WarningCategory).where(
            WarningCategory.organization_id == organization_id
        )
        return (await db.execute(stmt)).scalar() or 0


warning_category_repository = WarningCategoryRepository()
