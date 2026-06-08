"""평가 서비스 — Evaluation v1 비즈니스 로직.

Evaluation Service — create/update/submit-gate/direction-validation/delete,
template seed helper, build_evaluation_response (joined names + average).

핵심 규칙:
    - org-scope: 모든 조회는 organization_id 로 격리.
    - 방향 검증: app.core.permissions.can_evaluate (priority 헬퍼, 매직넘버 금지).
    - 스냅샷: 작성 시 조직 Basic 템플릿 config 를 template_snapshot 으로 deep-copy.
              job_title 은 선택 position 이름의 스냅샷.
    - submit-gate: status='submitted' 면 9개 criteria 전부 + 각 1..5 강제.
    - soft delete: deleted_at. 읽기는 항상 deleted_at IS NULL.
    - average: rated 항목 평균(1-dp), 응답 0개면 None (항상 계산).
"""

from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.evaluation import (
    BASIC_TEMPLATE_NAME,
    build_default_config,
)
from app.core.permissions import can_evaluate, role_priority
from app.models.evaluation import EvalTemplate, Evaluation
from app.models.organization import Store
from app.models.user import User
from app.models.work import Position
from app.repositories.evaluation_repository import (
    eval_template_repository,
    evaluation_repository,
)
from app.schemas.evaluation import EvaluationCreate, EvaluationUpdate
from app.utils.exceptions import BadRequestError, NotFoundError


class EvaluationService:
    """평가 서비스 — 템플릿 시드/조회 + 평가 CRUD + 응답 빌드."""

    # ====================================================================
    # 템플릿 시드 / 조회
    # ====================================================================

    async def ensure_basic_template(
        self, db: AsyncSession, organization_id: UUID
    ) -> EvalTemplate:
        """조직의 기본 Basic 템플릿(is_default)을 보장하고 반환. Idempotent.

        이미 있으면 그대로 반환, 없으면 §2.1 BASIC config 로 insert.
        v1 에서 템플릿이 생성되는 유일한 경로(startup 시드 + 신규 org).
        commit 은 호출자(시드 훅 / org setup) 책임 — 여기서는 flush 만.
        """
        existing = await eval_template_repository.get_default(db, organization_id)
        if existing is not None:
            return existing

        template = EvalTemplate(
            organization_id=organization_id,
            name=BASIC_TEMPLATE_NAME,
            config=build_default_config(),
            is_default=True,
            status="published",
            version=1,
            is_current=True,
            created_by_user_id=None,
        )
        db.add(template)
        await db.flush()
        await db.refresh(template)
        return template

    async def list_templates(
        self, db: AsyncSession, organization_id: UUID
    ) -> Sequence[EvalTemplate]:
        """조직 템플릿 목록. 없으면 Basic 을 보장(생성)한 뒤 반환."""
        templates = await eval_template_repository.list_by_org(db, organization_id)
        if not templates:
            await self.ensure_basic_template(db, organization_id)
            await db.commit()
            templates = await eval_template_repository.list_by_org(db, organization_id)
        return templates

    async def get_template(
        self, db: AsyncSession, template_id: UUID, organization_id: UUID
    ) -> EvalTemplate:
        """템플릿 단건 조회. org 밖/부재 시 404."""
        template = await eval_template_repository.get_by_id_org(
            db, template_id, organization_id
        )
        if template is None:
            raise NotFoundError("Evaluation template not found")
        return template

    def build_template_response(self, template: EvalTemplate) -> dict:
        """EvalTemplate → EvalTemplateResponse dict."""
        return {
            "id": str(template.id),
            "name": template.name,
            "is_default": template.is_default,
            "status": template.status,
            "version": template.version,
            "config": template.config,
            "created_at": template.created_at,
            "updated_at": template.updated_at,
        }

    # ====================================================================
    # 평가 가능 직원 (picker)
    # ====================================================================

    async def list_evaluatable_users(
        self,
        db: AsyncSession,
        current_user: User,
        *,
        store_id: UUID | None = None,
        q: str | None = None,
        page: int = 1,
        limit: int = 30,
    ) -> dict:
        """방향 필터된 평가 가능 직원 목록 (paginated envelope) + stores[].

        roles.priority > current_user priority (엄격히 더 낮은 권한),
        org-scope, 활성, 자기 제외. store_id 가 주어지면 그 매장 배정자만,
        q 가 주어지면 full_name/employee_no 부분일치(서버 검색). 매장 접근
        검증은 라우터에서 선행한다.

        N+1 제거(§P1): repository 가 role + user_stores→store 를 eager load
        하므로 여기서는 추가 DB 쿼리 없이 primary store(가장 먼저 배정된
        user_stores)와 stores[] 를 파이썬에서 만든다.
        """
        page = max(1, page)
        limit = max(1, min(limit, 100))
        q_clean = q.strip() if q else None

        users, total = await evaluation_repository.list_evaluatable_users(
            db,
            current_user.organization_id,
            min_priority_exclusive=role_priority(current_user),
            exclude_user_id=current_user.id,
            store_id=store_id,
            q=q_clean,
            limit=limit,
            offset=(page - 1) * limit,
        )

        org_id = current_user.organization_id
        items: list[dict] = []
        for u in users:
            # org-scope: 후보의 user_stores 중 같은 org store 만, created_at ASC.
            assigned = [
                us
                for us in u.user_stores
                if us.store is not None and us.store.organization_id == org_id
            ]
            assigned.sort(key=lambda us: us.created_at)
            stores = [
                {"id": str(us.store_id), "name": us.store.name} for us in assigned
            ]
            primary = assigned[0] if assigned else None

            items.append(
                {
                    "id": str(u.id),
                    "full_name": u.full_name,
                    "employee_no": u.employee_no,
                    "role_name": u.role.name if u.role else "",
                    "role_priority": role_priority(u),
                    "store_id": str(primary.store_id) if primary else None,
                    "store_name": primary.store.name if primary else None,
                    # user_stores 에 position 컬럼이 없어 prefill 불가 — None (best-effort).
                    "position_id": None,
                    "position_name": None,
                    "stores": stores,
                }
            )

        return {
            "items": items,
            "total": total,
            "page": page,
            "limit": limit,
            "has_more": (page * limit) < total,
        }

    # ====================================================================
    # 평가 조회
    # ====================================================================

    async def list_evaluations(
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
        return await evaluation_repository.list_active(
            db,
            organization_id,
            store_ids=store_ids,
            status=status,
            evaluatee_id=evaluatee_id,
            page=page,
            per_page=per_page,
        )

    async def get_evaluation(
        self, db: AsyncSession, evaluation_id: UUID, organization_id: UUID
    ) -> Evaluation:
        """평가 단건 조회 (org-scope + soft-delete 제외). 부재 시 404."""
        evaluation = await evaluation_repository.get_active(
            db, evaluation_id, organization_id
        )
        if evaluation is None:
            raise NotFoundError("Evaluation not found")
        return evaluation

    # ====================================================================
    # 평가 생성 / 수정
    # ====================================================================

    async def _load_evaluatee(
        self, db: AsyncSession, evaluatee_id: UUID, organization_id: UUID
    ) -> User:
        """org-scope 로 피평가자 로드 (role eager). 부재 시 404."""
        from sqlalchemy.orm import selectinload

        result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(
                User.id == evaluatee_id,
                User.organization_id == organization_id,
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise NotFoundError("Evaluatee not found")
        return user

    async def _validate_store_org(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID
    ) -> Store:
        """store 가 caller org 에 속하는지 검증 후 반환.

        org 불일치/부재 → 404 (cross-org 존재 누설 방지, §4). Owner 의 경우
        check_store_access 가 no-op 이므로 org 격리는 반드시 여기서 강제한다.
        """
        store = await db.get(Store, store_id)
        if store is None or store.organization_id != organization_id:
            raise NotFoundError("Store not found")
        return store

    async def _resolve_position_name(
        self,
        db: AsyncSession,
        position_id: UUID,
        store_id: UUID,
        organization_id: UUID,
    ) -> str:
        """position 이 store 에 속하고 org 범위인지 검증 후 이름 반환.

        position 은 org 컬럼이 없어 소속 store 를 통해 org 를 확인한다
        (Store.organization_id == organization_id). store 불일치 → 400.
        org 밖 store 는 _validate_store_org 가 선행 404 처리.
        job_title 스냅샷 원본.
        """
        position = await db.get(Position, position_id)
        if position is None or position.store_id != store_id:
            raise BadRequestError("Position does not belong to the selected store")
        # position 의 소속 store 가 caller org 인지 확인 (org 격리).
        await self._validate_store_org(db, position.store_id, organization_id)
        return position.name

    def _validate_response_codes(
        self, responses: dict[str, int], template_snapshot: dict
    ) -> None:
        """responses 의 모든 key 가 snapshot criteria code 인지 검증. 아니면 400."""
        valid_codes = {c["code"] for c in template_snapshot.get("criteria", [])}
        unknown = set(responses) - valid_codes
        if unknown:
            raise BadRequestError(
                f"Unknown criterion code(s): {', '.join(sorted(unknown))}"
            )

    def _enforce_submit_gate(
        self, responses: dict[str, int], template_snapshot: dict
    ) -> None:
        """submit 시 9개 criteria 전부 + 각 1..max_score 존재 강제. 미달 → 400."""
        for c in template_snapshot.get("criteria", []):
            code = c["code"]
            max_score = c.get("max_score", 5)
            value = responses.get(code)
            if not isinstance(value, int) or isinstance(value, bool):
                raise BadRequestError(
                    "All 9 criteria must be rated (1–5) to submit"
                )
            if not (1 <= value <= max_score):
                raise BadRequestError(
                    "All 9 criteria must be rated (1–5) to submit"
                )

    def _enforce_submit_period(
        self, period_start: date | None, period_end: date | None
    ) -> None:
        """submit 시 기간 필수 + 미래 금지 + start<=end 강제 (§M5). 미달 → 400."""
        today = datetime.now(timezone.utc).date()
        if period_start is None or period_end is None:
            raise BadRequestError("Evaluation period is required to submit")
        if period_end < period_start:
            raise BadRequestError("period_end must be on or after period_start")
        if period_start > today or period_end > today:
            raise BadRequestError("Evaluation period cannot be in the future")

    async def create_evaluation(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        evaluator: User,
        data: EvaluationCreate,
    ) -> Evaluation:
        """새 평가 생성. 방향 검증 + 스냅샷 + (submit 시) submit-gate.

        draft 는 부분 저장 허용: evaluatee_id 만 필수. store/position/period/
        responses 는 optional. submit 은 store_id + 비미래 기간 + 9개 전부 강제.
        매장 접근 검증은 라우터에서 선행한다(store_id 있을 때만).
        """
        evaluatee_id = UUID(data.evaluatee_id)
        store_id = UUID(data.store_id) if data.store_id else None
        position_id = UUID(data.position_id) if data.position_id else None

        # submit 인데 store 없으면 거부 (§M6).
        if data.status == "submitted" and store_id is None:
            raise BadRequestError("Store is required to submit")

        # store org 격리 — Owner 는 check_store_access 가 no-op 이므로 여기서 강제.
        if store_id is not None:
            await self._validate_store_org(db, store_id, organization_id)

        # 방향 검증 — 평가자보다 엄격히 낮은 권한만.
        evaluatee = await self._load_evaluatee(db, evaluatee_id, organization_id)
        if not can_evaluate(evaluator, evaluatee):
            from fastapi import HTTPException

            raise HTTPException(
                status_code=403,
                detail="You can only evaluate users with lower authority",
            )

        # 템플릿 스냅샷 — 조직 Basic config deep-copy.
        template = await self.ensure_basic_template(db, organization_id)
        template_snapshot = deepcopy(template.config)

        # job_title 스냅샷 — position 검증 후 이름. position 은 store 가 있을 때만 유효.
        job_title: str | None = None
        if position_id is not None:
            if store_id is None:
                raise BadRequestError("Store is required when a position is selected")
            job_title = await self._resolve_position_name(
                db, position_id, store_id, organization_id
            )

        responses = dict(data.responses)
        self._validate_response_codes(responses, template_snapshot)

        submitted_at: datetime | None = None
        if data.status == "submitted":
            self._enforce_submit_period(data.period_start, data.period_end)
            self._enforce_submit_gate(responses, template_snapshot)
            submitted_at = datetime.now(timezone.utc)

        try:
            evaluation = Evaluation(
                organization_id=organization_id,
                evaluator_id=evaluator.id,
                evaluatee_id=evaluatee_id,
                store_id=store_id,
                position_id=position_id,
                job_title=job_title,
                template_id=template.id,
                template_snapshot=template_snapshot,
                period_start=data.period_start,
                period_end=data.period_end,
                responses=responses,
                improvement=data.improvement,
                good_examples=data.good_examples,
                status=data.status,
                submitted_at=submitted_at,
            )
            db.add(evaluation)
            await db.flush()
            await db.refresh(evaluation)
            await db.commit()
            return evaluation
        except Exception:
            await db.rollback()
            raise

    async def update_evaluation(
        self,
        db: AsyncSession,
        *,
        evaluation_id: UUID,
        organization_id: UUID,
        current_user: User,
        data: EvaluationUpdate,
        check_store_access,
    ) -> Evaluation:
        """평가 수정 (draft/submitted 양쪽). partial update.

        evaluatee/store 변경 시 방향·매장 재검증. position 변경 시 job_title 재스냅샷.
        template_snapshot 은 v1 에서 변경하지 않는다(채점 기준 고정).
        draft→submitted 전환 시 submit-gate + submitted_at stamp.
        check_store_access: 라우터가 주입하는 async (store_id) → None | raise 403.
        """
        evaluation = await self.get_evaluation(db, evaluation_id, organization_id)
        fields = data.model_dump(exclude_unset=True)

        try:
            # store 변경 — org 격리 + 접근 검증 후 반영. None 명시 시 draft 에서 clear.
            if "store_id" in fields:
                if fields["store_id"] is not None:
                    new_store_id = UUID(fields["store_id"])
                    # org 격리 — Owner 는 check_store_access 가 no-op 이라 여기서 강제.
                    await self._validate_store_org(db, new_store_id, organization_id)
                    await check_store_access(new_store_id)
                    evaluation.store_id = new_store_id
                else:
                    # store 를 비우면 position/job_title 도 함께 비운다 (정합성).
                    evaluation.store_id = None
                    evaluation.position_id = None
                    evaluation.job_title = None

            # evaluatee 변경 — 방향 재검증.
            if "evaluatee_id" in fields and fields["evaluatee_id"] is not None:
                new_evaluatee_id = UUID(fields["evaluatee_id"])
                evaluatee = await self._load_evaluatee(
                    db, new_evaluatee_id, organization_id
                )
                if not can_evaluate(current_user, evaluatee):
                    from fastapi import HTTPException

                    raise HTTPException(
                        status_code=403,
                        detail="You can only evaluate users with lower authority",
                    )
                evaluation.evaluatee_id = new_evaluatee_id

            # position 변경 — job_title 재스냅샷 (snapshot 은 유지).
            if "position_id" in fields:
                new_position_id = (
                    UUID(fields["position_id"]) if fields["position_id"] else None
                )
                evaluation.position_id = new_position_id
                if new_position_id is not None:
                    if evaluation.store_id is None:
                        raise BadRequestError(
                            "Store is required when a position is selected"
                        )
                    evaluation.job_title = await self._resolve_position_name(
                        db,
                        new_position_id,
                        evaluation.store_id,
                        organization_id,
                    )
                else:
                    evaluation.job_title = None

            # period — None 명시 시 draft 에서 clear 허용 (M6).
            if "period_start" in fields:
                evaluation.period_start = data.period_start
            if "period_end" in fields:
                evaluation.period_end = data.period_end
            if "improvement" in fields:
                evaluation.improvement = fields["improvement"]
            if "good_examples" in fields:
                evaluation.good_examples = fields["good_examples"]

            if "responses" in fields and fields["responses"] is not None:
                responses = dict(fields["responses"])
                self._validate_response_codes(responses, evaluation.template_snapshot)
                evaluation.responses = responses

            # 기간 정합성 — 둘 다 있는 경우에만 start<=end + 미래 금지 (§M5).
            today = datetime.now(timezone.utc).date()
            if (
                evaluation.period_start is not None
                and evaluation.period_end is not None
                and evaluation.period_end < evaluation.period_start
            ):
                raise BadRequestError("period_end must be on or after period_start")
            if (
                evaluation.period_start is not None
                and evaluation.period_start > today
            ) or (
                evaluation.period_end is not None and evaluation.period_end > today
            ):
                raise BadRequestError("Evaluation period cannot be in the future")

            # status 전환.
            if "status" in fields and fields["status"] is not None:
                new_status = fields["status"]
                if new_status == "submitted":
                    # submit 게이트 — store + 비미래 기간 + 9개 전부 (§M5/M6).
                    if evaluation.store_id is None:
                        raise BadRequestError("Store is required to submit")
                    self._enforce_submit_period(
                        evaluation.period_start, evaluation.period_end
                    )
                    self._enforce_submit_gate(
                        evaluation.responses, evaluation.template_snapshot
                    )
                    if evaluation.status != "submitted":
                        evaluation.submitted_at = datetime.now(timezone.utc)
                    evaluation.status = "submitted"
                else:
                    evaluation.status = "draft"

            evaluation.updated_at = datetime.now(timezone.utc)
            await db.flush()
            await db.refresh(evaluation)
            await db.commit()
            return evaluation
        except Exception:
            await db.rollback()
            raise

    async def delete_evaluation(
        self, db: AsyncSession, evaluation_id: UUID, organization_id: UUID
    ) -> None:
        """소프트 삭제. 이미 삭제/부재면 404 (idempotent-safe)."""
        evaluation = await self.get_evaluation(db, evaluation_id, organization_id)
        try:
            evaluation.deleted_at = datetime.now(timezone.utc)
            await db.flush()
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    # ====================================================================
    # 응답 빌드
    # ====================================================================

    @staticmethod
    def compute_average(responses: dict[str, int]) -> float | None:
        """rated 항목 평균(1-dp). 0개면 None."""
        if not responses:
            return None
        values = list(responses.values())
        mean = sum(values) / len(values)
        return round(mean * 10) / 10

    async def build_evaluation_response(
        self, db: AsyncSession, evaluation: Evaluation
    ) -> dict:
        """Evaluation → EvaluationResponse dict (joined names + average)."""
        evaluatee_name: str | None = None
        employee_no: str | None = None
        if evaluation.evaluatee_id:
            evaluatee = await db.get(User, evaluation.evaluatee_id)
            if evaluatee:
                evaluatee_name = evaluatee.full_name
                employee_no = evaluatee.employee_no

        evaluator_name: str | None = None
        if evaluation.evaluator_id:
            evaluator = await db.get(User, evaluation.evaluator_id)
            if evaluator:
                evaluator_name = evaluator.full_name

        store_name: str | None = None
        if evaluation.store_id:
            store = await db.get(Store, evaluation.store_id)
            if store:
                store_name = store.name

        position_name: str | None = None
        if evaluation.position_id:
            position = await db.get(Position, evaluation.position_id)
            if position:
                position_name = position.name

        responses: dict[str, int] = evaluation.responses or {}

        return {
            "id": str(evaluation.id),
            "status": evaluation.status,
            "evaluatee_id": str(evaluation.evaluatee_id) if evaluation.evaluatee_id else None,
            "evaluatee_name": evaluatee_name,
            "employee_no": employee_no,
            "evaluator_id": str(evaluation.evaluator_id) if evaluation.evaluator_id else None,
            "evaluator_name": evaluator_name,
            "store_id": str(evaluation.store_id) if evaluation.store_id else None,
            "store_name": store_name,
            "position_id": str(evaluation.position_id) if evaluation.position_id else None,
            "position_name": position_name,
            "job_title": evaluation.job_title,
            "period_start": evaluation.period_start,
            "period_end": evaluation.period_end,
            "template_id": str(evaluation.template_id) if evaluation.template_id else None,
            "template_snapshot": evaluation.template_snapshot,
            "responses": responses,
            "average": self.compute_average(responses),
            "improvement": evaluation.improvement,
            "good_examples": evaluation.good_examples,
            "created_at": evaluation.created_at,
            "updated_at": evaluation.updated_at,
            "submitted_at": evaluation.submitted_at,
        }


# 싱글턴 인스턴스
evaluation_service: EvaluationService = EvaluationService()
