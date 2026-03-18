"""체크리스트 템플릿 연결 레포지토리 — DEPRECATED.

cl_template_links 테이블은 Phase 1A에서 삭제되었습니다.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


class TemplateLinkRepository:
    """DEPRECATED — cl_template_links table was dropped in Phase 1A."""

    async def get_by_id(self, *args, **kwargs):
        return None

    async def get_by_template(self, *args, **kwargs):
        return []

    async def get_by_store(self, *args, **kwargs):
        return []

    async def check_duplicate(self, *args, **kwargs):
        return False

    async def create(self, *args, **kwargs):
        return None

    async def delete(self, *args, **kwargs):
        return False


# 싱글턴 인스턴스
template_link_repository: TemplateLinkRepository = TemplateLinkRepository()
