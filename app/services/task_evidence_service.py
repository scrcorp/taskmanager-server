"""업무 증빙 서비스 — 업무 증빙 비즈니스 로직.

Task Evidence Service — Business logic for task evidence management.
Handles evidence creation (with assignee verification), listing, deletion,
and response building with resolved user names.
"""

from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import TaskEvidence
from app.models.user import User
from app.repositories.task_evidence_repository import task_evidence_repository
from app.repositories.task_repository import task_repository
from app.utils.exceptions import ForbiddenError, NotFoundError


class TaskEvidenceService:
    """업무 증빙 서비스.

    Task evidence service providing evidence creation, listing,
    deletion, and response building.
    """

    async def add_evidence(
        self,
        db: AsyncSession,
        task_id: UUID,
        user_id: UUID,
        file_url: str,
        file_type: str = "photo",
        note: str | None = None,
    ) -> TaskEvidence:
        """업무 증빙을 추가합니다. 담당자만 가능.

        Add evidence to an additional task. Only assignees can submit evidence.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            task_id: 업무 UUID (Task UUID)
            user_id: 제출자 UUID (Submitter user UUID)
            file_url: 파일 URL (Uploaded file URL)
            file_type: 파일 유형 (File type: "photo" or "document")
            note: 메모, 선택 (Optional note)

        Returns:
            TaskEvidence: 생성된 증빙 (Created evidence record)

        Raises:
            NotFoundError: 업무가 없을 때 (When task not found)
            ForbiddenError: 담당자가 아닐 때 (When user is not an assignee)
        """
        # 업무 존재 확인 — Verify task exists
        task = await task_repository.get_by_id(db, task_id)
        if task is None:
            raise NotFoundError("추가 업무를 찾을 수 없습니다 (Additional task not found)")

        # 담당자 확인 — Verify user is an assignee
        assignee = await task_repository.get_assignee(db, task_id, user_id)
        if assignee is None:
            raise ForbiddenError(
                "이 업무의 담당자만 증빙을 추가할 수 있습니다 (Only assignees can add evidence)"
            )

        # 증빙 생성 — Create evidence record
        evidence: TaskEvidence = await task_evidence_repository.create(
            db,
            {
                "task_id": task_id,
                "user_id": user_id,
                "file_url": file_url,
                "file_type": file_type,
                "note": note,
            },
        )
        return evidence

    async def get_evidences(
        self,
        db: AsyncSession,
        task_id: UUID,
    ) -> list[dict]:
        """업무의 모든 증빙을 사용자 이름과 함께 조회합니다.

        List all evidences for a task with resolved user names.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            task_id: 업무 UUID (Task UUID)

        Returns:
            list[dict]: 증빙 응답 딕셔너리 목록 (List of evidence response dicts)
        """
        evidences: Sequence[TaskEvidence] = await task_evidence_repository.get_by_task_id(
            db, task_id
        )
        results: list[dict] = []
        for evidence in evidences:
            response: dict = await self.build_response(db, evidence)
            results.append(response)
        return results

    async def delete_evidence(
        self,
        db: AsyncSession,
        evidence_id: UUID,
        user_id: UUID,
    ) -> None:
        """본인의 증빙을 삭제합니다.

        Delete own evidence. Only the submitter can delete their evidence.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            evidence_id: 증빙 UUID (Evidence UUID)
            user_id: 요청 사용자 UUID (Requesting user UUID)

        Raises:
            NotFoundError: 증빙이 없을 때 (When evidence not found)
            ForbiddenError: 본인의 증빙이 아닐 때 (When evidence belongs to another user)
        """
        evidence: TaskEvidence | None = await task_evidence_repository.get_by_id(
            db, evidence_id
        )
        if evidence is None:
            raise NotFoundError("증빙을 찾을 수 없습니다 (Evidence not found)")

        # 본인 확인 — Verify ownership
        if evidence.user_id != user_id:
            raise ForbiddenError(
                "본인의 증빙만 삭제할 수 있습니다 (Can only delete your own evidence)"
            )

        await task_evidence_repository.delete(db, evidence)

    async def build_response(
        self,
        db: AsyncSession,
        evidence: TaskEvidence,
    ) -> dict:
        """증빙 응답 딕셔너리를 구성합니다 (사용자 이름 포함).

        Build evidence response dict with resolved user name.

        Args:
            db: 비동기 데이터베이스 세션 (Async database session)
            evidence: 증빙 ORM 객체 (Evidence ORM object)

        Returns:
            dict: 사용자 이름이 포함된 응답 딕셔너리
                  (Response dict with user name)
        """
        # 제출자 이름 조회 — Fetch submitter name
        user_result = await db.execute(
            select(User.full_name).where(User.id == evidence.user_id)
        )
        user_name: str | None = user_result.scalar()

        return {
            "id": str(evidence.id),
            "task_id": str(evidence.task_id),
            "user_id": str(evidence.user_id),
            "user_name": user_name,
            "file_url": evidence.file_url,
            "file_type": evidence.file_type,
            "note": evidence.note,
            "created_at": evidence.created_at,
        }


# 싱글턴 인스턴스 — Singleton instance
task_evidence_service: TaskEvidenceService = TaskEvidenceService()
