"""페이지네이션 유틸리티 모듈.

Pagination utility module for SQLAlchemy async queries.
Provides a generic paginate function and a Page response model
for consistent pagination across all list endpoints.
"""

from typing import Any, Sequence, TypeVar
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


class Page(BaseModel):
    """페이지네이션 결과 모델.

    Pagination result model for typed responses.
    Contains the paginated items and metadata for client-side pagination controls.

    Attributes:
        items: 현재 페이지 항목 목록 (Items for the current page)
        total: 전체 항목 수 (Total count across all pages)
        page: 현재 페이지 번호 (Current page number, 1-based)
        per_page: 페이지당 항목 수 (Items per page)
        pages: 전체 페이지 수 (Total number of pages)
    """

    items: list[Any]  # 현재 페이지 항목 목록 (Paginated items)
    total: int  # 전체 항목 수 (Total item count)
    page: int  # 현재 페이지 번호 — 1부터 시작 (Current page, 1-indexed)
    per_page: int  # 페이지당 항목 수 (Items per page)
    pages: int  # 전체 페이지 수 (Total pages, computed: ceil(total/per_page))


async def paginate(
    db: AsyncSession,
    query: Select[Any],
    page: int = 1,
    per_page: int = 20,
) -> tuple[Sequence[Any], int]:
    """SQLAlchemy 쿼리에 대한 페이지네이션을 수행합니다.

    Execute a paginated SQLAlchemy query, returning items and total count.
    Runs two queries: one for the total count (via subquery) and one for
    the actual page of results with OFFSET/LIMIT.

    Args:
        db: 비동기 DB 세션 (Async database session)
        query: SQLAlchemy Select 쿼리 (Base query to paginate)
        page: 요청 페이지 번호, 1부터 시작 (Page number, 1-indexed, default: 1)
        per_page: 페이지당 항목 수 (Items per page, default: 20)

    Returns:
        tuple[Sequence[Any], int]: (항목 목록, 전체 개수) 튜플
            (Tuple of paginated items and total count)
    """
    # 전체 개수 조회 — 서브쿼리로 감싸서 COUNT 실행 (Count total via subquery)
    count_query = select(func.count()).select_from(query.subquery())
    total: int = (await db.execute(count_query)).scalar() or 0

    # 페이지 항목 조회 — OFFSET/LIMIT 적용 (Fetch page items with offset/limit)
    offset: int = (page - 1) * per_page
    result = await db.execute(query.offset(offset).limit(per_page))
    items: Sequence[Any] = result.scalars().all()

    return items, total
