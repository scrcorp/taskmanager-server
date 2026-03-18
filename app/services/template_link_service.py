"""체크리스트 템플릿 연결 서비스 — DEPRECATED.

cl_template_links 테이블은 Phase 1A에서 삭제되었습니다.
이 서비스는 호환성 유지를 위해 남겨두었으나 실제로는 동작하지 않습니다.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.exceptions import NotFoundError


class TemplateLinkService:
    """DEPRECATED — cl_template_links table was dropped in Phase 1A."""

    async def create_link(self, *args, **kwargs):
        raise NotFoundError("Template links feature has been removed")

    async def list_links(self, *args, **kwargs):
        return []

    async def delete_link(self, *args, **kwargs):
        raise NotFoundError("Template links feature has been removed")

    async def build_response(self, db: AsyncSession, link) -> dict:
        return {}


# 싱글턴 인스턴스
template_link_service: TemplateLinkService = TemplateLinkService()
