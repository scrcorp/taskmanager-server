"""사번 이력 레포지토리 — append-only ledger 조회/추가.

Employee number history repository.
Provides the two operations needed to enforce permanent burn:
    - exists_for_org: 해당 org 에서 사번이 이미 사용된 적 있는지
    - add: 새 사번을 이력에 기록 (최초 부여 메타 포함)

All queries are organization-scoped.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee_no_history import EmployeeNoHistory
from app.repositories.base import BaseRepository


class EmployeeNoHistoryRepository(BaseRepository[EmployeeNoHistory]):
    """사번 이력 테이블 쿼리 레포지토리.

    Repository handling the append-only employee number history ledger.
    """

    def __init__(self) -> None:
        super().__init__(EmployeeNoHistory)

    async def exists_for_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
        employee_no: str,
    ) -> bool:
        """해당 org 에서 사번이 이미 burn 됐는지 확인합니다.

        Check whether the given employee number has ever been used (burned)
        within the organization.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)
            employee_no: 정규화된 사번 (Normalized employee number)

        Returns:
            bool: 이력에 존재(=burn)하면 True (True if already burned)
        """
        result = await db.execute(
            select(func.count())
            .select_from(EmployeeNoHistory)
            .where(
                EmployeeNoHistory.organization_id == organization_id,
                EmployeeNoHistory.employee_no == employee_no,
            )
        )
        return (result.scalar() or 0) > 0

    async def add(
        self,
        db: AsyncSession,
        organization_id: UUID,
        employee_no: str,
        first_assigned_user_id: UUID | None,
    ) -> EmployeeNoHistory:
        """새 사번을 이력에 기록합니다 (append-only).

        Append a new employee number to the ledger (burn it).
        Flushes within the current transaction; does NOT commit.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            organization_id: 조직 ID (Organization UUID)
            employee_no: 정규화된 사번 (Normalized employee number)
            first_assigned_user_id: 최초 부여 대상 유저 (Audit, nullable)

        Returns:
            EmployeeNoHistory: 생성된 이력 레코드 (Created ledger row)
        """
        row = EmployeeNoHistory(
            organization_id=organization_id,
            employee_no=employee_no,
            first_assigned_user_id=first_assigned_user_id,
        )
        db.add(row)
        await db.flush()
        return row


# 싱글턴 인스턴스 — Singleton instance
employee_no_history_repository: EmployeeNoHistoryRepository = EmployeeNoHistoryRepository()
