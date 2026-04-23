"""Access Code 관리 유틸 — service_key 별 단일 활성 코드.

Generic helpers for the `access_codes` table. Any service can gate external
access with a rotatable short code.

Bootstrap:
    - `.env` 에 `{SERVICE}_ACCESS_CODE` (대문자) 가 있으면 upsert(source='env')
    - 없고 DB 에도 row 없으면 랜덤 6자 생성 (source='auto')
    - 있고 .env 에 없으면 DB 값 유지
"""

from __future__ import annotations

import os
import secrets
import string
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.access_code import AccessCode


# 6자 영숫자 (혼동 방지 위해 0/O, 1/I/l 제외)
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_code(length: int = 6) -> str:
    """랜덤 access code 생성 (대문자 영숫자, 혼동 문자 제외)."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


async def get_code(db: AsyncSession, service_key: str) -> AccessCode | None:
    """service_key 에 해당하는 access code 조회."""
    result = await db.execute(
        select(AccessCode).where(AccessCode.service_key == service_key)
    )
    return result.scalar_one_or_none()


async def verify_code(db: AsyncSession, service_key: str, submitted: str) -> bool:
    """제출된 코드가 service_key 의 현재 코드와 일치하는지 확인."""
    record = await get_code(db, service_key)
    if record is None:
        return False
    # 대소문자 무관 비교 — 사용자 입력 편의성
    return secrets.compare_digest(record.code.upper(), (submitted or "").upper())


async def ensure_code(db: AsyncSession, service_key: str, env_var_name: str | None = None) -> AccessCode:
    """startup 시 호출 — .env 또는 DB 값으로 코드 보장.

    1. env_var_name 이 지정되고 값이 있으면 → upsert(source='env')
    2. 아니면 DB 확인 → 있으면 그대로 반환
    3. 없으면 랜덤 생성 → INSERT(source='auto')

    Args:
        db: 비동기 세션 (commit 은 호출자가 책임)
        service_key: 예 "attendance"
        env_var_name: 예 "ATTENDANCE_ACCESS_CODE"
    """
    env_value = os.getenv(env_var_name) if env_var_name else None
    existing = await get_code(db, service_key)

    if env_value:
        env_value_clean = env_value.strip().upper()
        if existing is None:
            record = AccessCode(
                service_key=service_key,
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
        code=generate_code(),
        source="auto",
    )
    db.add(record)
    await db.flush()
    return record


async def rotate_code(db: AsyncSession, service_key: str) -> AccessCode:
    """수동 rotate — admin 엔드포인트에서 호출. source='auto' 로 전환."""
    record = await get_code(db, service_key)
    if record is None:
        record = AccessCode(service_key=service_key, code=generate_code(), source="auto")
        db.add(record)
    else:
        record.code = generate_code()
        record.source = "auto"
        record.rotated_at = datetime.now(timezone.utc)
    await db.flush()
    return record
