"""업무 증빙 레포지토리 — 업무 증빙 관련 DB 쿼리 담당.

Task Evidence Repository — Handles all task-evidence-related database queries.
Provides CRUD operations for photo/document evidence attached to additional tasks.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import TaskEvidence


class TaskEvidenceRepository:
    """업무 증빙 레포지토리.

    Task evidence repository providing query methods
    for evidence records associated with additional tasks.
    """

    async def get_by_task_id(
        self,
        db: AsyncSession,
        task_id: UUID,
    ) -> Sequence[TaskEvidence]:
        """특정 업무의 모든 증빙을 조회합니다.

        Retrieve all evidences for a specific additional task.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            task_id: 업무 UUID (Task UUID)

        Returns:
            Sequence[TaskEvidence]: 증빙 목록 (List of evidence records)
        """
        query: Select = (
            select(TaskEvidence)
            .where(TaskEvidence.task_id == task_id)
            .order_by(TaskEvidence.created_at.desc())
        )
        result = await db.execute(query)
        return result.scalars().all()

    async def get_by_id(
        self,
        db: AsyncSession,
        evidence_id: UUID,
    ) -> TaskEvidence | None:
        """ID로 증빙을 조회합니다.

        Retrieve a single evidence by its UUID.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            evidence_id: 증빙 UUID (Evidence UUID)

        Returns:
            TaskEvidence | None: 증빙 레코드 또는 None (Evidence record or None)
        """
        query: Select = select(TaskEvidence).where(TaskEvidence.id == evidence_id)
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def create(
        self,
        db: AsyncSession,
        data: dict,
    ) -> TaskEvidence:
        """새 증빙 레코드를 생성합니다.

        Create a new evidence record.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            data: 증빙 데이터 딕셔너리 (Evidence data dictionary)

        Returns:
            TaskEvidence: 생성된 증빙 (Created evidence record)
        """
        evidence: TaskEvidence = TaskEvidence(**data)
        db.add(evidence)
        await db.flush()
        await db.refresh(evidence)
        return evidence

    async def delete(
        self,
        db: AsyncSession,
        evidence: TaskEvidence,
    ) -> None:
        """증빙 레코드를 삭제합니다.

        Delete an evidence record.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            evidence: 삭제할 증빙 ORM 객체 (Evidence ORM object to delete)
        """
        await db.delete(evidence)
        await db.flush()


# 싱글턴 인스턴스 — Singleton instance
task_evidence_repository: TaskEvidenceRepository = TaskEvidenceRepository()
