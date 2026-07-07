"""Access Code 관리 유틸 — (service_key, organization) 별 단일 활성 코드.

Generic helpers for the `access_codes` table. 각 조직은 서비스별로 자기 코드를
하나 가진다. `code` 값은 service_key 안에서 전역 유니크이므로, 제출된 코드
하나만으로 조직을 역조회할 수 있다 (`resolve_org_by_code`).

Bootstrap (조직별):
    - `ensure_code(db, service_key, organization_id)` → 없으면 랜덤 6자 생성(source='auto')
    - env override 는 단일 org 하위호환용 (env_var_name 지정 시 해당 org 코드를 env 값으로)
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.access_code import AccessCode
from app.models.organization import Organization


# 6자 영숫자 (혼동 방지 위해 0/O, 1/I/l 제외)
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_MAX_GEN_TRIES = 20


def generate_code(length: int = 6) -> str:
    """랜덤 access code 생성 (대문자 영숫자, 혼동 문자 제외)."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


async def _code_taken(db: AsyncSession, service_key: str, code: str) -> bool:
    """service_key 안에서 code 가 이미 쓰이는지 (전역 유니크 보장용)."""
    existing = await db.execute(
        select(AccessCode.id).where(
            AccessCode.service_key == service_key, AccessCode.code == code
        )
    )
    return existing.first() is not None


async def generate_unique_code(db: AsyncSession, service_key: str, length: int = 6) -> str:
    """service_key 안에서 충돌하지 않는 코드 생성 (충돌 시 재시도).

    코드 공간(30^6 ≈ 7억)이 조직 수보다 압도적으로 커서 사실상 1회에 성공하나,
    만일을 대비해 재시도한다. 극단적으로 실패하면 길이를 늘려 보장한다.
    """
    for _ in range(_MAX_GEN_TRIES):
        candidate = generate_code(length)
        if not await _code_taken(db, service_key, candidate):
            return candidate
    # 방어적: 재시도 다 실패하면 길이를 늘려 재귀 (충돌 확률 사실상 0)
    return await generate_unique_code(db, service_key, length + 1)


async def get_code(
    db: AsyncSession, service_key: str, organization_id: UUID | None = None
) -> AccessCode | None:
    """(service_key, organization_id) 에 해당하는 access code 조회."""
    result = await db.execute(
        select(AccessCode).where(
            AccessCode.service_key == service_key,
            AccessCode.organization_id == organization_id,
        )
    )
    return result.scalar_one_or_none()


async def resolve_org_by_code(
    db: AsyncSession, service_key: str, submitted: str
) -> UUID | None:
    """제출된 코드로 조직을 역조회. 매치 없으면 None.

    태블릿 등록 흐름의 핵심 — 회사코드 없이 코드 하나로 org 를 확정한다.
    대소문자 무관 비교(입력 편의). code 는 service_key 내 유니크라 최대 1행.
    """
    normalized = (submitted or "").strip().upper()
    if not normalized:
        return None
    result = await db.execute(
        select(AccessCode.organization_id).where(
            AccessCode.service_key == service_key,
            AccessCode.code == normalized,
        )
    )
    return result.scalar_one_or_none()


async def ensure_code(
    db: AsyncSession,
    service_key: str,
    organization_id: UUID | None,
    env_var_name: str | None = None,
) -> AccessCode:
    """조직별 코드 보장 — 없으면 생성.

    1. env_var_name 이 지정되고 값이 있으면 → 해당 org 코드를 env 값으로 upsert(source='env')
    2. 아니면 org 에 코드가 있으면 그대로 반환
    3. 없으면 유니크 랜덤 생성 → INSERT(source='auto')

    Args:
        db: 비동기 세션 (commit 은 호출자가 책임)
        service_key: 예 "attendance"
        organization_id: 대상 조직
        env_var_name: 예 "ATTENDANCE_ACCESS_CODE" (단일 org 하위호환용, 보통 미사용)
    """
    env_value = os.getenv(env_var_name) if env_var_name else None
    existing = await get_code(db, service_key, organization_id)

    if env_value:
        env_value_clean = env_value.strip().upper()
        if existing is None:
            record = AccessCode(
                service_key=service_key,
                organization_id=organization_id,
                code=env_value_clean,
                source="env",
            )
            db.add(record)
            await db.flush()
            return record
        if existing.code != env_value_clean or existing.source != "env":
            existing.code = env_value_clean
            existing.source = "env"
            existing.rotated_at = datetime.now(timezone.utc)
            await db.flush()
        return existing

    if existing is not None:
        return existing

    record = AccessCode(
        service_key=service_key,
        organization_id=organization_id,
        code=await generate_unique_code(db, service_key),
        source="auto",
    )
    db.add(record)
    await db.flush()
    return record


async def ensure_codes_for_all_orgs(db: AsyncSession, service_key: str) -> int:
    """활성 조직 전체에 코드 보장 (startup 보정용). 새로 생성한 개수 반환.

    이 기능 도입 전 만들어졌거나 코드 없이 생성된 org 를 커버한다.
    """
    org_ids = (
        await db.execute(select(Organization.id).where(Organization.is_active == True))  # noqa: E712
    ).scalars().all()
    created = 0
    for oid in org_ids:
        existing = await get_code(db, service_key, oid)
        if existing is None:
            await ensure_code(db, service_key, oid)
            created += 1
    return created


async def rotate_code(
    db: AsyncSession, service_key: str, organization_id: UUID | None
) -> AccessCode:
    """수동 rotate — admin 엔드포인트에서 호출 (자기 org 코드만). source='auto' 로 전환."""
    record = await get_code(db, service_key, organization_id)
    new_code = await generate_unique_code(db, service_key)
    if record is None:
        record = AccessCode(
            service_key=service_key,
            organization_id=organization_id,
            code=new_code,
            source="auto",
        )
        db.add(record)
    else:
        record.code = new_code
        record.source = "auto"
        record.rotated_at = datetime.now(timezone.utc)
    await db.flush()
    return record
