"""휴게 규칙 서비스."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schedule import StoreBreakRule
from app.repositories.break_rule_repository import break_rule_repository
from app.repositories.store_repository import store_repository
from app.schemas.schedule import BreakRuleResponse, BreakRuleUpsert
from app.utils.exceptions import NotFoundError


class BreakRuleService:

    def _to_response(self, rule: StoreBreakRule) -> BreakRuleResponse:
        return BreakRuleResponse(
            id=str(rule.id),
            store_id=str(rule.store_id),
            max_continuous_minutes=rule.max_continuous_minutes,
            break_duration_minutes=rule.break_duration_minutes,
            max_daily_work_minutes=rule.max_daily_work_minutes,
            work_hour_calc_basis=rule.work_hour_calc_basis,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )

    async def _verify_store(
        self, db: AsyncSession, store_id: UUID, organization_id: UUID
    ) -> None:
        store = await store_repository.get_by_id(db, store_id, organization_id)
        if store is None:
            raise NotFoundError("Store not found")

    async def get_break_rule(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
    ) -> BreakRuleResponse | None:
        await self._verify_store(db, store_id, organization_id)
        rule = await break_rule_repository.get_by_store(db, store_id)
        if rule is None:
            return None
        return self._to_response(rule)

    async def upsert_break_rule(
        self,
        db: AsyncSession,
        store_id: UUID,
        organization_id: UUID,
        data: BreakRuleUpsert,
    ) -> BreakRuleResponse:
        try:
            await self._verify_store(db, store_id, organization_id)
            existing = await break_rule_repository.get_by_store(db, store_id)

            if existing is not None:
                updated = await break_rule_repository.update(
                    db, existing.id, data.model_dump()
                )
                result = self._to_response(updated)  # type: ignore[arg-type]
            else:
                created = await break_rule_repository.create(
                    db,
                    {
                        "store_id": store_id,
                        **data.model_dump(),
                    },
                )
                result = self._to_response(created)

            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise


break_rule_service: BreakRuleService = BreakRuleService()
