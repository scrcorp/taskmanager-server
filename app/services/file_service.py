"""범용 파일 서비스 — files/file_usages 레지스트리 GC.

File registry service. 런타임 삭제 경로는 `file_usages` 행만 지우고 blob 은 안 건드린다.
이 서비스의 GC 가 "어떤 usage 도 가리키지 않는 files"(NOT EXISTS)를 찾아 blob + 행을 회수한다.
중앙 file_usages 테이블 덕에 refcount=0 판정이 한 쿼리로 끝난다.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, exists
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file import File, FileUsage
from app.services.storage_service import storage_service


class FileService:
    """파일 레지스트리 서비스."""

    async def gc_orphan_files(
        self,
        db: AsyncSession,
        *,
        older_than_minutes: int = 0,
        limit: int | None = None,
    ) -> int:
        """어떤 file_usages 도 가리키지 않는 files 행을 회수한다 (blob 삭제 + 행 삭제).

        Args:
            older_than_minutes: 0 보다 크면 그만큼 지난 files 만 대상(직접 업로드 in-flight 보호).
                우리 쓰기 경로는 files+usage 를 한 트랜잭션으로 만들어 0 으로도 안전하나,
                presigned temp 등 미래 흐름 대비로 스케줄 잡은 임계값을 줄 수 있다.
            limit: 한 번에 회수할 최대 행 수 (배치 처리).

        Returns:
            회수한 files 행 수.
        """
        stmt = select(File).where(
            ~exists(select(FileUsage.id).where(FileUsage.file_id == File.id))
        )
        if older_than_minutes > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
            stmt = stmt.where(File.created_at < cutoff)
        if limit is not None:
            stmt = stmt.limit(limit)

        orphans = list((await db.scalars(stmt)).all())
        for f in orphans:
            if f.path:
                storage_service.delete_file(f.path)
            await db.delete(f)
        if orphans:
            await db.flush()
        return len(orphans)


file_service = FileService()
