"""경고 레포지토리 — Warning v1 DB 쿼리 (순수 DB, 비즈니스 로직 없음).

Warning Repository — pure SQLAlchemy queries for warnings.
모든 쿼리는 organization_id 로 org-scope 되고, 읽기는 항상 deleted_at IS NULL 로
soft-delete 를 제외한다. 이름 join 은 service 가 별도로 처리.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import Role, User
from app.models.user_store import UserStore
from app.models.warning import Warning
from app.repositories.base import BaseRepository


class WarningRepository(BaseRepository[Warning]):
    """warnings CRUD. 읽기는 항상 deleted_at IS NULL, org-scope."""

    def __init__(self) -> None:
        super().__init__(Warning)

    async def next_seq(self, db: AsyncSession, organization_id: UUID) -> int:
        """조직 내 다음 일련번호 = max(seq)+1.

        soft-deleted 행도 포함해 max 를 잡아 seq 재사용을 막는다(사람 ID 안정성).
        """
        result = await db.execute(
            select(func.coalesce(func.max(Warning.seq), 0)).where(
                Warning.organization_id == organization_id
            )
        )
        return int(result.scalar() or 0) + 1

    async def get_active(
        self, db: AsyncSession, warning_id: UUID, organization_id: UUID
    ) -> Warning | None:
        """org-scope + soft-delete 제외한 단일 경고 조회."""
        query: Select = select(Warning).where(
            Warning.id == warning_id,
            Warning.organization_id == organization_id,
            Warning.deleted_at.is_(None),
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def list_active(
        self,
        db: AsyncSession,
        organization_id: UUID,
        *,
        store_ids: list[UUID] | None = None,
        status: str | None = None,
        category: str | None = None,
        subject_user_id: UUID | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Warning], int]:
        """org-scope + soft-delete 제외 목록. created_at DESC.

        store_ids=None → 매장 필터 없음(Owner 전체). 빈 리스트면 결과 0건.
        category 가 주어지면 categories ARRAY 에 그 코드가 포함된 경고만.
        """
        base = select(Warning).where(
            Warning.organization_id == organization_id,
            Warning.deleted_at.is_(None),
        )
        if store_ids is not None:
            if not store_ids:
                return [], 0
            base = base.where(Warning.store_id.in_(store_ids))
        if status is not None:
            base = base.where(Warning.status == status)
        if category is not None:
            base = base.where(Warning.categories.any(category))
        if subject_user_id is not None:
            base = base.where(Warning.subject_user_id == subject_user_id)

        count_result = await db.execute(select(func.count()).select_from(base.subquery()))
        total: int = count_result.scalar() or 0

        query = (
            base.order_by(Warning.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        result = await db.execute(query)
        return list(result.scalars().all()), total

    async def counts_by_subject(
        self,
        db: AsyncSession,
        organization_id: UUID,
        *,
        store_ids: list[UUID] | None = None,
    ) -> dict[UUID, tuple[int, int]]:
        """직원별 (total, active) 경고 갯수 — Staff 목록 칼럼용.

        org-scope + soft-delete 제외. store_ids=None → 전체(Owner),
        빈 리스트 → 빈 dict. subject_user_id 가 NULL 인 행(직원 삭제됨)은 제외.
        반환: {subject_user_id: (total, active)}.
        """
        if store_ids is not None and not store_ids:
            return {}

        active_expr = func.sum(
            case((Warning.status == "active", 1), else_=0)
        )
        base = (
            select(
                Warning.subject_user_id,
                func.count().label("total"),
                active_expr.label("active"),
            )
            .where(
                Warning.organization_id == organization_id,
                Warning.deleted_at.is_(None),
                Warning.subject_user_id.is_not(None),
            )
            .group_by(Warning.subject_user_id)
        )
        if store_ids is not None:
            base = base.where(Warning.store_id.in_(store_ids))

        result = await db.execute(base)
        return {
            row.subject_user_id: (int(row.total), int(row.active or 0))
            for row in result.all()
        }

    async def subject_warning_ordinal(
        self,
        db: AsyncSession,
        organization_id: UUID,
        subject_user_id: UUID,
        created_at,
    ) -> int:
        """대상 직원의 경고 순번 (1-based) — PDF 의 First/Second/Other 결정용.

        org-scope + soft-delete 제외, created_at <= 기준(자기 포함) 개수.
        철회 여부와 무관하게 발행 순서 기준(총 몇 번째 경고인지).
        """
        result = await db.execute(
            select(func.count()).where(
                Warning.organization_id == organization_id,
                Warning.subject_user_id == subject_user_id,
                Warning.deleted_at.is_(None),
                Warning.created_at <= created_at,
            )
        )
        return int(result.scalar() or 1)

    async def list_my_active(
        self,
        db: AsyncSession,
        organization_id: UUID,
        subject_user_id: UUID,
        *,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Warning], int]:
        """대상 직원 본인의 active 경고 목록 (paginated). created_at DESC.

        org-scope + soft-delete 제외 + status='active' + subject == 본인.
        앱(직원)이 자기 경고만 본다 — withdrawn 은 노출하지 않는다.
        """
        base = select(Warning).where(
            Warning.organization_id == organization_id,
            Warning.subject_user_id == subject_user_id,
            Warning.deleted_at.is_(None),
            Warning.status == "active",
        )
        count_result = await db.execute(select(func.count()).select_from(base.subquery()))
        total: int = count_result.scalar() or 0
        query = (
            base.order_by(Warning.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        result = await db.execute(query)
        return list(result.scalars().all()), total

    async def get_my_active(
        self,
        db: AsyncSession,
        warning_id: UUID,
        organization_id: UUID,
        subject_user_id: UUID,
    ) -> Warning | None:
        """대상 직원 본인의 단일 active 경고 (org-scope + soft-delete 제외 + 본인 소유)."""
        result = await db.execute(
            select(Warning).where(
                Warning.id == warning_id,
                Warning.organization_id == organization_id,
                Warning.subject_user_id == subject_user_id,
                Warning.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def count_my_unsigned(
        self,
        db: AsyncSession,
        organization_id: UUID,
        subject_user_id: UUID,
    ) -> int:
        """본인의 active 경고 중 employee 서명 행이 없는 갯수 (badge 용).

        LEFT JOIN warning_signatures (party='employee') 후 NULL 인 행 count.
        """
        from app.models.warning_signature import WarningSignature

        sig_subq = (
            select(WarningSignature.warning_id)
            .where(WarningSignature.party == "employee")
            .subquery()
        )
        result = await db.execute(
            select(func.count())
            .select_from(Warning)
            .outerjoin(sig_subq, sig_subq.c.warning_id == Warning.id)
            .where(
                Warning.organization_id == organization_id,
                Warning.subject_user_id == subject_user_id,
                Warning.deleted_at.is_(None),
                Warning.status == "active",
                sig_subq.c.warning_id.is_(None),
            )
        )
        return int(result.scalar() or 0)

    async def list_warnable_users(
        self,
        db: AsyncSession,
        organization_id: UUID,
        *,
        min_priority_exclusive: int,
        exclude_user_id: UUID,
        store_id: UUID | None = None,
        q: str | None = None,
        limit: int = 30,
        offset: int = 0,
    ) -> tuple[Sequence[User], int]:
        """경고 대상 후보 직원 목록 (paginated) + total.

        roles.priority > min_priority_exclusive (엄격히 더 낮은 권한),
        org-scope, is_active=true AND deleted_at IS NULL, 자기자신 제외.
        store_id 가 주어지면 해당 매장에 배정된(user_stores) 직원만.
        q 가 주어지면 full_name OR employee_no 부분일치(대소문자 무시).

        N+1 제거: role 과 user_stores→store 를 한 번에 eager load.
        full_name ASC 정렬, (users, total) 반환. (evaluation picker 와 동형)
        """
        base = (
            select(User)
            .join(Role, User.role_id == Role.id)
            .where(
                User.organization_id == organization_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
                User.id != exclude_user_id,
                Role.priority > min_priority_exclusive,
            )
        )
        if store_id is not None:
            base = base.join(UserStore, UserStore.user_id == User.id).where(
                UserStore.store_id == store_id
            )
        if q:
            pattern = f"%{q}%"
            base = base.where(
                or_(
                    User.full_name.ilike(pattern),
                    User.employee_no.ilike(pattern),
                )
            )

        count_subq = base.with_only_columns(User.id).distinct().subquery()
        count_result = await db.execute(select(func.count()).select_from(count_subq))
        total: int = count_result.scalar() or 0

        page_query = (
            base.options(
                selectinload(User.role),
                selectinload(User.user_stores).selectinload(UserStore.store),
            )
            .order_by(User.full_name.asc())
            .offset(offset)
            .limit(limit)
        )
        result = await db.execute(page_query)
        return list(result.scalars().unique().all()), total


warning_repository: WarningRepository = WarningRepository()
