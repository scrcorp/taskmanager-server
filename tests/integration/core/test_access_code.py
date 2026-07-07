"""app/core/access_code — (service_key, organization) 별 코드 로직 (DB 사용).

검증:
- generate_unique_code: service_key 안에서 유니크한 코드 생성
- ensure_code: 없으면 생성 / 있으면 idempotent 반환 / 조직별 분리
- get_code: 조직 스코프 (다른 org 코드 안 섞임)
- resolve_org_by_code: 코드 → org 역조회 (대소문자 무관 / 공백/미매치 → None)
- rotate_code: 새 유니크 코드 + 다른 org 코드에 영향 없음(격리)
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.core.access_code import (
    ensure_code,
    generate_unique_code,
    get_code,
    resolve_org_by_code,
    rotate_code,
)
from app.models.access_code import AccessCode
from app.models.organization import Organization

SVC = "attendance"


async def _mk_org(db, name: str) -> uuid.UUID:
    org = Organization(name=name)
    db.add(org)
    await db.flush()
    return org.id


async def _cleanup(db, org_ids: list[uuid.UUID]) -> None:
    await db.execute(delete(Organization).where(Organization.id.in_(org_ids)))
    await db.commit()


async def test_ensure_code_creates_then_idempotent(db):
    oid = await _mk_org(db, "AC Ensure Org")
    try:
        rec1 = await ensure_code(db, SVC, oid)
        assert rec1.code and rec1.organization_id == oid and rec1.source == "auto"
        rec2 = await ensure_code(db, SVC, oid)
        assert rec2.code == rec1.code  # 재호출은 같은 코드 (새로 안 만듦)
    finally:
        await _cleanup(db, [oid])


async def test_get_code_is_org_scoped(db):
    a = await _mk_org(db, "AC Org A")
    b = await _mk_org(db, "AC Org B")
    try:
        rec_a = await ensure_code(db, SVC, a)
        rec_b = await ensure_code(db, SVC, b)
        assert rec_a.code != rec_b.code  # 조직별 다른 코드
        assert (await get_code(db, SVC, a)).code == rec_a.code
        assert (await get_code(db, SVC, b)).code == rec_b.code
    finally:
        await _cleanup(db, [a, b])


async def test_resolve_org_by_code_roundtrip_and_case_insensitive(db):
    oid = await _mk_org(db, "AC Resolve Org")
    try:
        rec = await ensure_code(db, SVC, oid)
        assert await resolve_org_by_code(db, SVC, rec.code) == oid
        # 대소문자 무관
        assert await resolve_org_by_code(db, SVC, rec.code.lower()) == oid
        # 앞뒤 공백 허용
        assert await resolve_org_by_code(db, SVC, f"  {rec.code}  ") == oid
    finally:
        await _cleanup(db, [oid])


async def test_resolve_org_by_code_miss_and_empty_return_none(db):
    assert await resolve_org_by_code(db, SVC, "NOPE99") is None
    assert await resolve_org_by_code(db, SVC, "") is None
    assert await resolve_org_by_code(db, SVC, "   ") is None


async def test_rotate_changes_code_and_isolated_per_org(db):
    a = await _mk_org(db, "AC Rotate A")
    b = await _mk_org(db, "AC Rotate B")
    try:
        code_a = (await ensure_code(db, SVC, a)).code
        code_b = (await ensure_code(db, SVC, b)).code
        rec_a2 = await rotate_code(db, SVC, a)
        assert rec_a2.code != code_a  # a 는 새 코드
        assert (await get_code(db, SVC, b)).code == code_b  # b 는 불변 (격리)
        # 옛 a 코드로는 더이상 a 를 못 찾고, 새 코드로 찾음
        assert await resolve_org_by_code(db, SVC, code_a) is None
        assert await resolve_org_by_code(db, SVC, rec_a2.code) == a
    finally:
        await _cleanup(db, [a, b])


async def test_generate_unique_code_avoids_collision(db):
    oid = await _mk_org(db, "AC Uniq Org")
    try:
        existing = (await ensure_code(db, SVC, oid)).code
        # 20회 생성 모두 기존 코드와 겹치지 않아야 함
        for _ in range(20):
            c = await generate_unique_code(db, SVC)
            assert c != existing
            assert len(c) == 6
    finally:
        await _cleanup(db, [oid])
