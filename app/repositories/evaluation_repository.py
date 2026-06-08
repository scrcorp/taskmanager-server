"""평가 레포지토리 — Evaluation v1 DB 쿼리 (순수 DB, 비즈니스 로직 없음).

Evaluation Repository — pure SQLAlchemy queries for eval_templates and evaluations.
모든 쿼리는 organization_id 로 org-scope 되고, evaluations 읽기는 항상
deleted_at IS NULL 로 soft-delete 를 제외한다. 이름 join 은 service 가 별도로 처리.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation import EvalTemplate, Evaluation
from app.models.user import Role, User
from app.models.user_store import UserStore
from app.repositories.base import BaseRepository


class EvalTemplateRepository(BaseRepository[EvalTemplate]):
    """eval_templates CRUD. v1 은 조직당 Basic 1개(is_default)만 다룬다."""

    def __init__(self) -> None:
        super().__init__(EvalTemplate)

    async def get_by_id_org(
        self, db: AsyncSession, template_id: UUID, organization_id: UUID
    ) -> EvalTemplate | None:
        """id + org 로 단일 템플릿 조회."""
        query: Select = select(EvalTemplate).where(
            EvalTemplate.id == template_id,
            EvalTemplate.organization_id == organization_id,
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def list_by_org(
        self, db: AsyncSession, organization_id: UUID
    ) -> Sequence[EvalTemplate]:
        """조직의 모든 템플릿 (v1 은 Basic 1개). version ASC 정렬."""
        query: Select = (
            select(EvalTemplate)
            .where(EvalTemplate.organization_id == organization_id)
            .order_by(EvalTemplate.version.asc())
        )
        result = await db.execute(query)
        return list(result.scalars().all())

    async def get_default(
        self, db: AsyncSession, organization_id: UUID
    ) -> EvalTemplate | None:
        """조직의 기본 템플릿(is_default=True) 조회. 시드/작성 시 스냅샷 원본."""
        query: Select = (
            select(EvalTemplate)
            .where(
                EvalTemplate.organization_id == organization_id,
                EvalTemplate.is_default.is_(True),
            )
            .order_by(EvalTemplate.version.asc())
            .limit(1)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()


class EvaluationRepository(BaseRepository[Evaluation]):
    """evaluations CRUD. 읽기는 항상 deleted_at IS NULL, org-scope."""

    def __init__(self) -> None:
        super().__init__(Evaluation)

    async def get_active(
        self, db: AsyncSession, evaluation_id: UUID, organization_id: UUID
    ) -> Evaluation | None:
        """org-scope + soft-delete 제외한 단일 평가 조회."""
        query: Select = select(Evaluation).where(
            Evaluation.id == evaluation_id,
            Evaluation.organization_id == organization_id,
            Evaluation.deleted_at.is_(None),
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
        evaluatee_id: UUID | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Evaluation], int]:
        """org-scope + soft-delete 제외 목록. created_at DESC.

        store_ids=None → 매장 필터 없음(Owner 전체). 빈 리스트면 결과 0건.
        """
        base = select(Evaluation).where(
            Evaluation.organization_id == organization_id,
            Evaluation.deleted_at.is_(None),
        )
        if store_ids is not None:
            if not store_ids:
                return [], 0
            base = base.where(Evaluation.store_id.in_(store_ids))
        if status is not None:
            base = base.where(Evaluation.status == status)
        if evaluatee_id is not None:
            base = base.where(Evaluation.evaluatee_id == evaluatee_id)

        count_result = await db.execute(select(func.count()).select_from(base.subquery()))
        total: int = count_result.scalar() or 0

        query = (
            base.order_by(Evaluation.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        result = await db.execute(query)
        return list(result.scalars().all()), total

    async def list_evaluatable_users(
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
        """평가 가능 후보 직원 목록 (paginated) + total.

        roles.priority > min_priority_exclusive (엄격히 더 낮은 권한),
        org-scope, is_active=true AND deleted_at IS NULL, 자기자신 제외.
        store_id 가 주어지면 해당 매장에 배정된(user_stores) 직원만.
        q 가 주어지면 full_name OR employee_no 부분일치(대소문자 무시).

        N+1 제거: role 과 user_stores→store 를 한 번에 eager load
        (selectinload). 호출자는 추가 쿼리 없이 primary/stores[] 를 만든다.
        full_name ASC 정렬, (users, total) 반환.
        """
        from sqlalchemy.orm import selectinload

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

        # total — 필터된 base 위에서 distinct user count (store join 시 중복 방지).
        count_subq = base.with_only_columns(User.id).distinct().subquery()
        count_result = await db.execute(
            select(func.count()).select_from(count_subq)
        )
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

    async def get_primary_store(
        self, db: AsyncSession, user_id: UUID
    ) -> UserStore | None:
        """후보의 primary store = 가장 먼저 배정된(created_at ASC) user_stores row.

        명시적 is_primary 컬럼이 없어 earliest-created 규칙을 사용한다.
        """
        query: Select = (
            select(UserStore)
            .where(UserStore.user_id == user_id)
            .order_by(UserStore.created_at.asc())
            .limit(1)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()


eval_template_repository: EvalTemplateRepository = EvalTemplateRepository()
evaluation_repository: EvaluationRepository = EvaluationRepository()
