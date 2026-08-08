"""운영자 지정 access code — 스키마 정규화/검증 + set_code 동작.

검증:
- AdminAccessCodeSetRequest: strip / upper 정규화, 내부 공백 거부, 길이 4~32
- set_code: 방어적 정규화, no-op(자기 코드 재제출), 타 org 점유 시 409
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete

from app.core.access_code import get_code, set_code
from app.models.organization import Organization
from app.schemas.attendance_device import AdminAccessCodeSetRequest

SVC = "attendance"


# ── AdminAccessCodeSetRequest — 정규화/검증 ─────────────────


def test_schema_normalizes_lowercase_and_strips():
    """소문자 → 대문자, 좌우 공백 strip."""
    req = AdminAccessCodeSetRequest(code="  myCode99  ")
    assert req.code == "MYCODE99"


def test_schema_rejects_internal_spaces():
    """내부 공백은 422 (strip 후에도 남는 공백)."""
    with pytest.raises(ValidationError) as exc_info:
        AdminAccessCodeSetRequest(code="AB CD")
    assert "Code cannot contain spaces." in str(exc_info.value)


def test_schema_length_boundaries():
    """길이 3 거부 / 4 허용 / 32 허용 / 33 거부."""
    with pytest.raises(ValidationError) as exc_info:
        AdminAccessCodeSetRequest(code="ABC")
    assert "Code must be at least 4 characters." in str(exc_info.value)

    assert AdminAccessCodeSetRequest(code="ABCD").code == "ABCD"
    assert AdminAccessCodeSetRequest(code="A" * 32).code == "A" * 32

    with pytest.raises(ValidationError):
        AdminAccessCodeSetRequest(code="A" * 33)


def test_schema_strip_before_length_check():
    """좌우 공백은 strip 후 길이를 재므로 ' ABCD ' 는 허용."""
    assert AdminAccessCodeSetRequest(code=" ABCD ").code == "ABCD"


# ── set_code — DB 동작 ─────────────────────────────────────


async def _mk_org(db, name: str) -> uuid.UUID:
    org = Organization(name=name)
    db.add(org)
    await db.flush()
    return org.id


async def _cleanup(db, org_ids: list[uuid.UUID]) -> None:
    # access_codes 는 organization FK CASCADE 로 같이 삭제됨
    await db.execute(delete(Organization).where(Organization.id.in_(org_ids)))
    await db.commit()


async def test_set_code_defensive_normalization(db):
    """스키마를 안 거친 호출도 strip + upper 로 방어적 정규화."""
    oid = await _mk_org(db, "AC Set Norm Org")
    try:
        rec = await set_code(db, SVC, oid, "  setcode1  ")
        assert rec.code == "SETCODE1"
        assert rec.source == "manual"
        assert rec.rotated_at is not None
        assert rec.organization_id == oid
    finally:
        await _cleanup(db, [oid])


async def test_set_code_noop_on_same_code(db):
    """자기 org 의 기존 코드와 같으면 그대로 반환 — rotated_at 불변."""
    oid = await _mk_org(db, "AC Set Noop Org")
    try:
        rec1 = await set_code(db, SVC, oid, "NOOPCODE")
        rotated_at_1 = rec1.rotated_at
        rec2 = await set_code(db, SVC, oid, "noopcode")  # 소문자 재제출도 no-op
        assert rec2.id == rec1.id
        assert rec2.code == "NOOPCODE"
        assert rec2.rotated_at == rotated_at_1
    finally:
        await _cleanup(db, [oid])


async def test_set_code_updates_existing_row(db):
    """기존 row 가 있으면 새 row 를 만들지 않고 코드만 교체 (upsert)."""
    oid = await _mk_org(db, "AC Set Update Org")
    try:
        rec1 = await set_code(db, SVC, oid, "FIRSTCODE")
        rec2 = await set_code(db, SVC, oid, "SECONDCODE")
        assert rec2.id == rec1.id
        assert rec2.code == "SECONDCODE"
        assert (await get_code(db, SVC, oid)).code == "SECONDCODE"
    finally:
        await _cleanup(db, [oid])


async def test_set_code_taken_by_other_org_409(db):
    """service_key 안에서 타 org 가 이미 쓰는 코드면 409 access_code_taken."""
    a = await _mk_org(db, "AC Set Taken A")
    b = await _mk_org(db, "AC Set Taken B")
    try:
        await set_code(db, SVC, a, "TAKENCODE")
        with pytest.raises(HTTPException) as exc_info:
            await set_code(db, SVC, b, "takencode")  # 대소문자 달라도 같은 코드
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["code"] == "access_code_taken"
        # b 의 코드는 만들어지지 않음
        assert await get_code(db, SVC, b) is None
    finally:
        await _cleanup(db, [a, b])
