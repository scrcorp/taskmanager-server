"""조직 레포지토리 — 조직 CRUD 쿼리.

Organization Repository — CRUD queries for organizations.
Extends BaseRepository with Organization-specific database operations.
"""

from app.models.organization import Organization
from app.repositories.base import BaseRepository


class OrganizationRepository(BaseRepository[Organization]):
    """조직 테이블에 대한 데이터베이스 쿼리를 담당하는 레포지토리.

    Repository handling database queries for the organizations table.
    Inherits generic CRUD from BaseRepository.
    """

    def __init__(self) -> None:
        """OrganizationRepository를 초기화합니다.

        Initialize the OrganizationRepository with the Organization model.
        """
        super().__init__(Organization)


# 싱글턴 인스턴스 — Singleton instance
organization_repository: OrganizationRepository = OrganizationRepository()
