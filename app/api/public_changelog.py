"""Public changelog 라우터 — 인증 없음. 발행분만 노출.

console/app/homepage 가 호출. homepage 는 category 생략(전체 집계),
console/app 은 자기 category 로 필터. draft 는 절대 노출하지 않는다.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.changelog import ChangelogPost
from app.repositories.changelog_repository import changelog_repository
from app.schemas.changelog import ChangelogCategory, ChangelogDetail, ChangelogListItem
from app.services.changelog_service import body_keys_to_urls
from app.services.storage_service import storage_service
from app.utils.pagination import Page, paginate

router: APIRouter = APIRouter(prefix="/changelog", tags=["Public Changelog"])


def _list_item(post: ChangelogPost) -> ChangelogListItem:
    return ChangelogListItem(
        slug=post.slug,
        category=post.category,
        title=post.title,
        summary=post.summary,
        cover_image_url=storage_service.resolve_url(post.cover_image_key),
        tags=post.tags or [],
        published_at=post.published_at,
    )


@router.get("/", response_model=Page)
async def list_published_changelog(
    db: AsyncSession = Depends(get_db),
    category: Optional[ChangelogCategory] = Query(
        None, description="제품 표면 필터. 생략 시 전체(homepage 집계)."
    ),
    q: Optional[str] = Query(None, description="제목/본문 검색"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
) -> Page:
    """발행된 changelog 목록 — 최신순, 카테고리/검색 필터, 페이지네이션."""
    query = changelog_repository.published_query(category, q)
    items, total = await paginate(db, query, page, per_page)
    pages = (total + per_page - 1) // per_page if per_page else 0
    return Page(
        items=[_list_item(p) for p in items],
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.get("/{slug}/", response_model=ChangelogDetail)
async def get_published_changelog(
    slug: str,
    db: AsyncSession = Depends(get_db),
) -> ChangelogDetail:
    """발행된 changelog 1건 (slug). 초안/미발행은 404."""
    post = await changelog_repository.get_published_by_slug(db, slug)
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return ChangelogDetail(
        slug=post.slug,
        category=post.category,
        title=post.title,
        summary=post.summary,
        body=body_keys_to_urls(post.body),
        cover_image_url=storage_service.resolve_url(post.cover_image_key),
        tags=post.tags or [],
        published_at=post.published_at,
    )
