"""Changelog — unit(service) + public API integration tests (merge gate).

핵심 불변식:
- 공개 GET 은 발행분(is_published)만 노출. draft 는 목록/상세 어디에도 안 보인다.
- category 필터 / q 검색 / 페이지네이션 정확성.
- service: slug 자동생성 + 전역 중복 회피, 발행 토글 시 published_at 설정.

changelog 는 글로벌(org 밖) 데이터라 org fixture 불필요.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.database import async_session
from app.main import app
from app.models.changelog import ChangelogPost
from app.schemas.changelog import ChangelogCreate, ChangelogUpdate
from app.services.changelog_service import (
    ChangelogNotFound,
    _slugify,
    body_keys_to_urls,
    body_urls_to_keys,
    changelog_service,
)
from app.services.storage_service import storage_service

import base64

# 최소 1x1 PNG (이미지 업로드/리졸브 테스트용)
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

PUBLIC = "/api/v1/public/changelog"
_PREFIX = "zztest-"  # 테스트가 만든 행만 정리하기 위한 slug prefix


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def seeded() -> list[ChangelogPost]:
    """published 3건(console 2 / staff_app 1) + draft 1건 시드. 정리 포함."""
    async with async_session() as db:
        await db.execute(delete(ChangelogPost).where(ChangelogPost.slug.like(f"{_PREFIX}%")))
        await db.commit()

        now = datetime.now(timezone.utc)
        rows = [
            ChangelogPost(slug=f"{_PREFIX}console-login", category="console",
                          title="Console login fix", summary="s0", body="Fixed login redirect",
                          tags=["bugfix"], is_published=True, published_at=now),
            ChangelogPost(slug=f"{_PREFIX}console-schedule", category="console",
                          title="Schedule builder", summary="s1", body="New schedule grid",
                          tags=["feature"], is_published=True, published_at=now),
            ChangelogPost(slug=f"{_PREFIX}staff-tips", category="staff_app",
                          title="Tips on app", summary="s2", body="Tip viewing",
                          tags=["feature"], is_published=True, published_at=now),
            ChangelogPost(slug=f"{_PREFIX}draft-secret", category="console",
                          title="Unreleased login secret", body="should never leak",
                          is_published=False),
        ]
        for r in rows:
            db.add(r)
        await db.commit()

    yield rows

    async with async_session() as db:
        await db.execute(delete(ChangelogPost).where(ChangelogPost.slug.like(f"{_PREFIX}%")))
        await db.commit()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_list_excludes_drafts(seeded):
    async with _client() as c:
        resp = await c.get(f"{PUBLIC}/")
    assert resp.status_code == 200
    data = resp.json()
    slugs = {it["slug"] for it in data["items"]}
    assert f"{_PREFIX}draft-secret" not in slugs
    assert data["total"] == 3
    # 목록 항목엔 body 가 없어야 한다(페이로드 절감 + draft 본문 유출 방지)
    assert "body" not in data["items"][0]


@pytest.mark.asyncio
async def test_category_filter(seeded):
    async with _client() as c:
        resp = await c.get(f"{PUBLIC}/", params={"category": "console"})
    data = resp.json()
    assert data["total"] == 2
    assert all(it["category"] == "console" for it in data["items"])


@pytest.mark.asyncio
async def test_search_q(seeded):
    async with _client() as c:
        resp = await c.get(f"{PUBLIC}/", params={"q": "schedule"})
    data = resp.json()
    slugs = {it["slug"] for it in data["items"]}
    assert slugs == {f"{_PREFIX}console-schedule"}


@pytest.mark.asyncio
async def test_search_does_not_leak_draft(seeded):
    """draft 본문에만 있는 단어로 검색해도 draft 는 안 잡힌다."""
    async with _client() as c:
        resp = await c.get(f"{PUBLIC}/", params={"q": "secret"})
    data = resp.json()
    # 'secret' 은 draft title/body 에만 존재 → published 결과 0
    assert all(not it["slug"].endswith("draft-secret") for it in data["items"])
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_detail_published(seeded):
    async with _client() as c:
        resp = await c.get(f"{PUBLIC}/{_PREFIX}console-login/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["body"] == "Fixed login redirect"
    assert data["category"] == "console"


@pytest.mark.asyncio
async def test_detail_draft_404(seeded):
    async with _client() as c:
        resp = await c.get(f"{PUBLIC}/{_PREFIX}draft-secret/")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pagination(seeded):
    async with _client() as c:
        resp = await c.get(f"{PUBLIC}/", params={"per_page": 2, "page": 1})
    data = resp.json()
    assert data["per_page"] == 2
    assert data["pages"] == 2
    assert len(data["items"]) == 2


# --------------------------------------------------------------------------- #
# Service unit
# --------------------------------------------------------------------------- #
def test_slugify():
    assert _slugify("Console Login Fix!") == "console-login-fix"
    assert _slugify("   ") == "post"


@pytest.mark.asyncio
async def test_create_dedupes_slug():
    async with async_session() as db:
        await db.execute(delete(ChangelogPost).where(ChangelogPost.slug.like(f"{_PREFIX}dup%")))
        await db.commit()
        p1 = await changelog_service.create(
            db, ChangelogCreate(title="Dup", category="console", body="b", slug=f"{_PREFIX}dup")
        )
        p2 = await changelog_service.create(
            db, ChangelogCreate(title="Dup", category="console", body="b", slug=f"{_PREFIX}dup")
        )
        await db.commit()
        assert p1.slug == f"{_PREFIX}dup"
        assert p2.slug == f"{_PREFIX}dup-2"
        await db.execute(delete(ChangelogPost).where(ChangelogPost.slug.like(f"{_PREFIX}dup%")))
        await db.commit()


@pytest.mark.asyncio
async def test_publish_sets_timestamp_and_toggles():
    async with async_session() as db:
        await db.execute(delete(ChangelogPost).where(ChangelogPost.slug.like(f"{_PREFIX}pub%")))
        await db.commit()
        post = await changelog_service.create(
            db, ChangelogCreate(title="Pub", category="homepage", body="b", slug=f"{_PREFIX}pub")
        )
        await db.commit()
        assert post.is_published is False and post.published_at is None

        await changelog_service.set_published(db, post.id, True)
        await db.commit()
        assert post.is_published is True and post.published_at is not None

        await changelog_service.set_published(db, post.id, False)
        await db.commit()
        assert post.is_published is False

        await db.execute(delete(ChangelogPost).where(ChangelogPost.slug.like(f"{_PREFIX}pub%")))
        await db.commit()


@pytest.mark.asyncio
async def test_update_missing_raises():
    import uuid
    async with async_session() as db:
        with pytest.raises(ChangelogNotFound):
            await changelog_service.update(
                db, uuid.uuid4(), ChangelogUpdate(title="x")
            )


# --------------------------------------------------------------------------- #
# 이미지 본문 round-trip (URL ↔ key) — P2
# --------------------------------------------------------------------------- #
def test_body_urls_to_keys_rewrites_bucket_url():
    key = "changelog/2026/06/29/abc.png"
    url = storage_service._build_url(key)  # 버킷 전체 URL
    md = f"intro\n\n![shot]({url})\n\ntail"
    out = body_urls_to_keys(md)
    assert f"![shot]({key})" in out
    assert url not in out


def test_body_urls_to_keys_leaves_external_url():
    md = "![logo](https://example.com/logo.png)"
    assert body_urls_to_keys(md) == md


def test_body_urls_to_keys_no_images_untouched():
    md = "# Title\n\nplain text, no images here."
    assert body_urls_to_keys(md) == md


def test_body_keys_to_urls_resolves_existing_key():
    # 실제 파일을 업로드해야 fallback 모드 resolve_url 이 URL 을 돌려준다.
    key = storage_service.upload_bytes(_PNG_1x1, "x.png", "changelog", "image/png")
    url = storage_service.resolve_url(key)
    assert url and url.startswith("http")
    md = f"![alt]({key})"
    out = body_keys_to_urls(md)
    assert f"![alt]({url})" in out
    # 역방향 round-trip
    assert body_urls_to_keys(out) == md


def test_body_keys_to_urls_leaves_absolute_url():
    md = "![alt](https://example.com/x.png)"
    assert body_keys_to_urls(md) == md


@pytest.mark.asyncio
async def test_create_update_store_relative_keys():
    """body 에 버킷 전체 URL 을 넣어도 DB 에는 상대경로(key)로 저장된다."""
    key = storage_service.upload_bytes(_PNG_1x1, "y.png", "changelog", "image/png")
    url = storage_service.resolve_url(key)
    assert url
    async with async_session() as db:
        await db.execute(delete(ChangelogPost).where(ChangelogPost.slug.like(f"{_PREFIX}img%")))
        await db.commit()
        post = await changelog_service.create(
            db,
            ChangelogCreate(
                title="Img", category="console",
                body=f"see ![p]({url})", slug=f"{_PREFIX}img",
            ),
        )
        await db.commit()
        assert f"![p]({key})" in post.body
        assert url not in post.body

        # update 도 동일하게 key 로 저장
        await changelog_service.update(
            db, post.id, ChangelogUpdate(body=f"edited ![q]({url})")
        )
        await db.commit()
        assert f"![q]({key})" in post.body
        assert url not in post.body

        await db.execute(delete(ChangelogPost).where(ChangelogPost.slug.like(f"{_PREFIX}img%")))
        await db.commit()


@pytest.mark.asyncio
async def test_public_detail_resolves_body_keys_to_urls():
    """공개 상세 응답의 body 는 상대경로 key 를 전체 URL 로 변환해 노출."""
    key = storage_service.upload_bytes(_PNG_1x1, "z.png", "changelog", "image/png")
    url = storage_service.resolve_url(key)
    assert url
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        await db.execute(delete(ChangelogPost).where(ChangelogPost.slug.like(f"{_PREFIX}resolve%")))
        await db.commit()
        db.add(ChangelogPost(
            slug=f"{_PREFIX}resolve", category="console", title="Resolve",
            body=f"pre ![c]({key}) post", is_published=True, published_at=now,
        ))
        await db.commit()
    try:
        async with _client() as c:
            resp = await c.get(f"{PUBLIC}/{_PREFIX}resolve/")
        assert resp.status_code == 200
        body = resp.json()["body"]
        assert url in body
        assert f"({key})" not in body  # 상대경로가 그대로 남으면 안 됨
    finally:
        async with async_session() as db:
            await db.execute(delete(ChangelogPost).where(ChangelogPost.slug.like(f"{_PREFIX}resolve%")))
            await db.commit()
