"""매장 그룹 서비스 — store_groups CRUD 비즈니스 로직.

Store Group Service — Business logic for store group CRUD.
그룹은 empid 채번 정책의 단위: numbering_mode="group" 이면 그룹 내 매장들이
시퀀스를 공유한다. 모드/편성 변경 시 기존 empid 중복은 경고만 하고 막지 않는다
(정책 A — 자동 재번호 금지, 해소는 EMPID 임포트에서).
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import NUMBERING_MODE_GROUP, Store, StoreGroup
from app.repositories.store_group_repository import store_group_repository
from app.schemas.organization import (
    StoreGroupCreate,
    StoreGroupResponse,
    StoreGroupUpdate,
)
from app.services.org_numbering import duplicate_empids_in_scope
from app.utils.exceptions import DuplicateError, NotFoundError


class StoreGroupService:
    """매장 그룹 비즈니스 로직을 처리하는 서비스."""

    @staticmethod
    def _to_response(
        group: StoreGroup,
        store_count: int = 0,
        duplicate_empids: list[dict[str, int]] | None = None,
    ) -> StoreGroupResponse:
        """StoreGroup 모델 → 응답 스키마 변환."""
        return StoreGroupResponse(
            id=str(group.id),
            organization_id=str(group.organization_id),
            name=group.name,
            sort_order=group.sort_order,
            numbering_mode=group.numbering_mode,
            number_range_start=group.number_range_start,
            store_count=store_count,
            duplicate_empids=duplicate_empids or [],
            created_at=group.created_at,
        )

    async def _group_store_ids(self, db: AsyncSession, group_id: UUID) -> list[UUID]:
        """그룹 소속 전체 매장 id (폐점 포함 — 번호 점유 유지)."""
        return list(
            (await db.execute(select(Store.id).where(Store.group_id == group_id)))
            .scalars()
            .all()
        )

    async def list_groups(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> list[StoreGroupResponse]:
        """조직의 그룹 목록 — sort_order 순, 소속 매장 수 포함."""
        groups = await store_group_repository.get_by_org(db, organization_id)
        counts = await store_group_repository.store_counts(db, organization_id)
        return [self._to_response(g, counts.get(g.id, 0)) for g in groups]

    async def create_group(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: StoreGroupCreate,
    ) -> StoreGroupResponse:
        """새 그룹을 생성합니다. org 내 이름 중복은 거절."""
        exists = await store_group_repository.exists(
            db, {"organization_id": organization_id, "name": data.name}
        )
        if exists:
            raise DuplicateError("A store group with this name already exists")
        next_sort = await store_group_repository.get_max_sort_order(db, organization_id) + 1
        try:
            group = await store_group_repository.create(
                db,
                {
                    "organization_id": organization_id,
                    "name": data.name,
                    "numbering_mode": data.numbering_mode,
                    "number_range_start": data.number_range_start,
                    "sort_order": next_sort,
                },
            )
            await db.commit()
            return self._to_response(group)
        except Exception:
            await db.rollback()
            raise

    async def update_group(
        self,
        db: AsyncSession,
        group_id: UUID,
        organization_id: UUID,
        data: StoreGroupUpdate,
    ) -> StoreGroupResponse:
        """그룹을 수정합니다. 공유 모드 전환 시 스코프 내 기존 empid 중복을 경고로 반환."""
        fields = data.model_dump(exclude_unset=True)
        # NOT NULL 컬럼(name/numbering_mode)에 명시적 null 이 오면 no-op 처리 (500 방지).
        # number_range_start 는 nullable — 명시적 null 로 번호대 해제 허용.
        fields = {k: v for k, v in fields.items() if v is not None or k == "number_range_start"}
        if "name" in fields and fields["name"] is not None:
            current = await store_group_repository.get_by_id(db, group_id, organization_id)
            if current is None:
                raise NotFoundError("Store group not found")
            if current.name != fields["name"]:
                exists = await store_group_repository.exists(
                    db, {"organization_id": organization_id, "name": fields["name"]}
                )
                if exists:
                    raise DuplicateError("A store group with this name already exists")
        try:
            group = await store_group_repository.update(
                db, group_id, fields, organization_id
            )
            if group is None:
                raise NotFoundError("Store group not found")
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        duplicates: list[dict[str, int]] = []
        if group.numbering_mode == NUMBERING_MODE_GROUP:
            scope = await self._group_store_ids(db, group.id)
            duplicates = await duplicate_empids_in_scope(db, scope)
        counts = await store_group_repository.store_counts(db, organization_id)
        return self._to_response(group, counts.get(group.id, 0), duplicates)

    async def delete_group(
        self,
        db: AsyncSession,
        group_id: UUID,
        organization_id: UUID,
    ) -> None:
        """그룹을 삭제합니다. 소속 매장은 FK SET NULL 로 미그룹 상태가 된다 (번호 불변)."""
        try:
            deleted = await store_group_repository.delete(db, group_id, organization_id)
            if not deleted:
                raise NotFoundError("Store group not found")
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def reorder_groups(
        self,
        db: AsyncSession,
        organization_id: UUID,
        ordered_ids: list[UUID],
    ) -> int:
        """그룹 표시 순서를 일괄 변경합니다."""
        try:
            updated = await store_group_repository.reorder(db, organization_id, ordered_ids)
            await db.commit()
            return updated
        except Exception:
            await db.rollback()
            raise


# 싱글턴 인스턴스 — Singleton instance
store_group_service: StoreGroupService = StoreGroupService()
