"""평가 레포지토리 — Evaluation CRUD.

Evaluation Repository — CRUD queries for eval_templates and evaluations tables.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.evaluation import EvalTemplate, EvalTemplateItem, Evaluation, EvalResponse
from app.repositories.base import BaseRepository


class EvalTemplateRepository(BaseRepository[EvalTemplate]):

    def __init__(self) -> None:
        super().__init__(EvalTemplate)

    async def get_with_items(
        self, db: AsyncSession, template_id: UUID, organization_id: UUID
    ) -> EvalTemplate | None:
        query: Select = (
            select(EvalTemplate)
            .options(selectinload(EvalTemplate.items))
            .where(
                EvalTemplate.id == template_id,
                EvalTemplate.organization_id == organization_id,
            )
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_by_org(
        self, db: AsyncSession, organization_id: UUID, page: int = 1, per_page: int = 20
    ) -> tuple[Sequence[EvalTemplate], int]:
        base = select(EvalTemplate).where(EvalTemplate.organization_id == organization_id)

        count_result = await db.execute(select(func.count()).select_from(base.subquery()))
        total: int = count_result.scalar() or 0

        query = base.order_by(EvalTemplate.created_at.desc()).offset((page - 1) * per_page).limit(per_page)
        result = await db.execute(query)
        return list(result.scalars().all()), total


class EvaluationRepository(BaseRepository[Evaluation]):

    def __init__(self) -> None:
        super().__init__(Evaluation)

    async def get_with_responses(
        self, db: AsyncSession, evaluation_id: UUID, organization_id: UUID
    ) -> Evaluation | None:
        query: Select = (
            select(Evaluation)
            .options(selectinload(Evaluation.responses))
            .where(
                Evaluation.id == evaluation_id,
                Evaluation.organization_id == organization_id,
            )
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_by_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
        evaluator_id: UUID | None = None,
        evaluatee_id: UUID | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Evaluation], int]:
        base = select(Evaluation).where(Evaluation.organization_id == organization_id)

        if evaluator_id:
            base = base.where(Evaluation.evaluator_id == evaluator_id)
        if evaluatee_id:
            base = base.where(Evaluation.evaluatee_id == evaluatee_id)
        if status:
            base = base.where(Evaluation.status == status)

        count_result = await db.execute(select(func.count()).select_from(base.subquery()))
        total: int = count_result.scalar() or 0

        query = base.order_by(Evaluation.created_at.desc()).offset((page - 1) * per_page).limit(per_page)
        result = await db.execute(query)
        return list(result.scalars().all()), total


eval_template_repository: EvalTemplateRepository = EvalTemplateRepository()
evaluation_repository: EvaluationRepository = EvaluationRepository()
