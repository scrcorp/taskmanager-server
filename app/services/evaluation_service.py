"""평가 서비스 — 평가 비즈니스 로직.

Evaluation Service — Business logic for evaluation template and evaluation management.
Handles template CRUD, evaluation creation, submission, and direction validation.
"""

from datetime import datetime, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation import EvalResponse, EvalTemplate, EvalTemplateItem, Evaluation
from app.models.organization import Store
from app.models.user import Role, User
from app.repositories.evaluation_repository import eval_template_repository, evaluation_repository
from app.schemas.evaluation import (
    EvalTemplateCreate,
    EvalTemplateUpdate,
    EvaluationCreate,
)
from app.utils.exceptions import BadRequestError, ForbiddenError, NotFoundError


class EvaluationService:
    """평가 서비스.

    Evaluation service providing template CRUD, evaluation CRUD,
    direction validation, and response building.
    """

    # === 템플릿 관리 ===

    async def list_templates(
        self,
        db: AsyncSession,
        organization_id: UUID,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[EvalTemplate], int]:
        return await eval_template_repository.get_by_org(db, organization_id, page, per_page)

    async def get_template(
        self,
        db: AsyncSession,
        template_id: UUID,
        organization_id: UUID,
    ) -> EvalTemplate:
        template = await eval_template_repository.get_with_items(db, template_id, organization_id)
        if template is None:
            raise NotFoundError("평가 템플릿을 찾을 수 없습니다 (Evaluation template not found)")
        return template

    async def create_template(
        self,
        db: AsyncSession,
        organization_id: UUID,
        data: EvalTemplateCreate,
    ) -> EvalTemplate:
        template = await eval_template_repository.create(db, {
            "organization_id": organization_id,
            "name": data.name,
            "target_role": data.target_role,
            "eval_type": data.eval_type,
            "cycle_weeks": data.cycle_weeks,
        })

        for item_data in data.items:
            item = EvalTemplateItem(
                template_id=template.id,
                title=item_data.title,
                type=item_data.type,
                max_score=item_data.max_score,
                sort_order=item_data.sort_order,
            )
            db.add(item)

        await db.flush()
        return await self.get_template(db, template.id, organization_id)

    async def update_template(
        self,
        db: AsyncSession,
        template_id: UUID,
        organization_id: UUID,
        data: EvalTemplateUpdate,
    ) -> EvalTemplate:
        template = await self.get_template(db, template_id, organization_id)

        if data.name is not None:
            template.name = data.name
        if data.target_role is not None:
            template.target_role = data.target_role
        if data.eval_type is not None:
            template.eval_type = data.eval_type
        if data.cycle_weeks is not None:
            template.cycle_weeks = data.cycle_weeks

        # items가 제공되면 기존 삭제 후 재생성
        if data.items is not None:
            for existing_item in template.items:
                await db.delete(existing_item)

            for item_data in data.items:
                item = EvalTemplateItem(
                    template_id=template.id,
                    title=item_data.title,
                    type=item_data.type,
                    max_score=item_data.max_score,
                    sort_order=item_data.sort_order,
                )
                db.add(item)

        template.updated_at = datetime.now(timezone.utc)
        await db.flush()
        return await self.get_template(db, template_id, organization_id)

    async def delete_template(
        self,
        db: AsyncSession,
        template_id: UUID,
        organization_id: UUID,
    ) -> bool:
        deleted = await eval_template_repository.delete(db, template_id, organization_id)
        if not deleted:
            raise NotFoundError("평가 템플릿을 찾을 수 없습니다 (Evaluation template not found)")
        return deleted

    def build_template_response(self, template: EvalTemplate) -> dict:
        items = []
        if hasattr(template, "items") and template.items:
            for item in template.items:
                items.append({
                    "id": str(item.id),
                    "title": item.title,
                    "type": item.type,
                    "max_score": item.max_score,
                    "sort_order": item.sort_order,
                })

        return {
            "id": str(template.id),
            "name": template.name,
            "target_role": template.target_role,
            "eval_type": template.eval_type,
            "cycle_weeks": template.cycle_weeks,
            "item_count": len(items),
            "items": items,
            "created_at": template.created_at,
            "updated_at": template.updated_at,
        }

    # === 평가 관리 ===

    async def _validate_direction(
        self,
        db: AsyncSession,
        evaluator_id: UUID,
        evaluatee_id: UUID,
    ) -> None:
        """평가 방향 검증 — 상위 → 하위만 가능."""
        evaluator_result = await db.execute(
            select(Role.priority)
            .join(User, User.role_id == Role.id)
            .where(User.id == evaluator_id)
        )
        evaluatee_result = await db.execute(
            select(Role.priority)
            .join(User, User.role_id == Role.id)
            .where(User.id == evaluatee_id)
        )
        evaluator_priority = evaluator_result.scalar()
        evaluatee_priority = evaluatee_result.scalar()

        if evaluator_priority is None or evaluatee_priority is None:
            raise NotFoundError("평가자 또는 피평가자를 찾을 수 없습니다")
        if evaluator_priority >= evaluatee_priority:
            raise ForbiddenError(
                "상위 역할만 하위 역할을 평가할 수 있습니다 "
                "(Only higher-role users can evaluate lower-role users)"
            )

    async def list_evaluations(
        self,
        db: AsyncSession,
        organization_id: UUID,
        evaluator_id: UUID | None = None,
        evaluatee_id: UUID | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[Sequence[Evaluation], int]:
        return await evaluation_repository.get_by_org(
            db, organization_id, evaluator_id, evaluatee_id, status, page, per_page
        )

    async def get_evaluation(
        self,
        db: AsyncSession,
        evaluation_id: UUID,
        organization_id: UUID,
    ) -> Evaluation:
        evaluation = await evaluation_repository.get_with_responses(db, evaluation_id, organization_id)
        if evaluation is None:
            raise NotFoundError("평가를 찾을 수 없습니다 (Evaluation not found)")
        return evaluation

    async def create_evaluation(
        self,
        db: AsyncSession,
        organization_id: UUID,
        evaluator_id: UUID,
        data: EvaluationCreate,
    ) -> Evaluation:
        evaluatee_id = UUID(data.evaluatee_id)
        template_id = UUID(data.template_id)
        store_id = UUID(data.store_id) if data.store_id else None

        # 방향 검증
        await self._validate_direction(db, evaluator_id, evaluatee_id)

        evaluation = await evaluation_repository.create(db, {
            "organization_id": organization_id,
            "evaluator_id": evaluator_id,
            "evaluatee_id": evaluatee_id,
            "template_id": template_id,
            "store_id": store_id,
            "status": "draft",
        })

        # 응답 생성
        for resp_data in data.responses:
            response = EvalResponse(
                evaluation_id=evaluation.id,
                template_item_id=UUID(resp_data.template_item_id),
                score=resp_data.score,
                text=resp_data.text,
            )
            db.add(response)

        await db.flush()
        return await self.get_evaluation(db, evaluation.id, organization_id)

    async def submit_evaluation(
        self,
        db: AsyncSession,
        evaluation_id: UUID,
        organization_id: UUID,
    ) -> Evaluation:
        evaluation = await self.get_evaluation(db, evaluation_id, organization_id)

        if evaluation.status != "draft":
            raise BadRequestError("작성 중인 평가만 제출할 수 있습니다 (Only draft evaluations can be submitted)")

        evaluation.status = "submitted"
        evaluation.submitted_at = datetime.now(timezone.utc)
        await db.flush()
        await db.refresh(evaluation)
        return evaluation

    async def build_evaluation_response(self, db: AsyncSession, evaluation: Evaluation) -> dict:
        # 평가자 이름
        evaluator_result = await db.execute(select(User.full_name).where(User.id == evaluation.evaluator_id))
        evaluator_name = evaluator_result.scalar() or "Unknown"

        # 피평가자 이름
        evaluatee_result = await db.execute(select(User.full_name).where(User.id == evaluation.evaluatee_id))
        evaluatee_name = evaluatee_result.scalar() or "Unknown"

        # 템플릿 이름
        template_name = None
        if evaluation.template_id:
            template_result = await db.execute(select(EvalTemplate.name).where(EvalTemplate.id == evaluation.template_id))
            template_name = template_result.scalar()

        # 매장 이름
        store_name = None
        if evaluation.store_id:
            store_result = await db.execute(select(Store.name).where(Store.id == evaluation.store_id))
            store_name = store_result.scalar()

        # 응답
        responses = []
        if hasattr(evaluation, "responses") and evaluation.responses:
            for resp in evaluation.responses:
                # 항목 제목
                item_result = await db.execute(
                    select(EvalTemplateItem.title).where(EvalTemplateItem.id == resp.template_item_id)
                )
                item_title = item_result.scalar()
                responses.append({
                    "id": str(resp.id),
                    "template_item_id": str(resp.template_item_id),
                    "item_title": item_title,
                    "score": resp.score,
                    "text": resp.text,
                })

        return {
            "id": str(evaluation.id),
            "evaluator_id": str(evaluation.evaluator_id),
            "evaluator_name": evaluator_name,
            "evaluatee_id": str(evaluation.evaluatee_id),
            "evaluatee_name": evaluatee_name,
            "template_id": str(evaluation.template_id) if evaluation.template_id else None,
            "template_name": template_name,
            "store_id": str(evaluation.store_id) if evaluation.store_id else None,
            "store_name": store_name,
            "status": evaluation.status,
            "responses": responses,
            "created_at": evaluation.created_at,
            "submitted_at": evaluation.submitted_at,
        }


# 싱글턴 인스턴스
evaluation_service: EvaluationService = EvaluationService()
