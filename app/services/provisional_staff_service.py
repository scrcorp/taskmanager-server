"""미가입(Provisional) 직원 계정 — 생성 / 인수 코드.

아직 앱에 가입하지 않은 직원도 관리자가 미리 만들어 두고 empid 부여·스케줄 배정을 할 수 있게 한다.
본인이 나중에 가입할 때 claim_code 로 **이 행을 그대로 인수**하므로 계정 병합이 필요 없다.

fail-closed 설계 (핵심):
    유령 = is_active=False + is_provisional=True.
    로그인(auth_service), 토큰 검증(api/deps), PIN 출근(attendance_device_service),
    알림(alert_service), 팁(tip_service), 리포트(report_service)가 전부 is_active 를
    게이트로 쓰므로 **자동으로 차단**된다. 스케줄 후보처럼 유령을 봐야 하는 곳만
    user_repository.get_by_org(filters={"include_provisional": True}) 로 명시 개방.
    → 새 코드에서 유령 제외를 깜빡해도 "노출"이 아니라 "제외"로 안전하게 실패한다.

추가 방어: clockin_pin 을 발급하지 않는다. PIN 조회가 lookup 키라 유령은 기기 출근 자체가 불가능.
"""

from __future__ import annotations

import secrets
import string
import uuid
from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import OWNER_PRIORITY, SUPER_OWNER_PRIORITY
from app.models.org_member import OrgMember
from app.models.user import Role, User
from app.repositories.role_repository import role_repository
from app.repositories.user_repository import user_repository
from app.utils.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.utils.password import hash_password

# 인수 코드 문자셋 — 혼동 문자(0/O, 1/I/L) 제외. 구두 전달·수기 입력을 견디게.
_CLAIM_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CLAIM_CODE_LENGTH = 8


def generate_claim_code() -> str:
    """인수 코드 1개 생성 (org 내 유일성은 호출부가 재시도로 보장)."""
    return "".join(secrets.choice(_CLAIM_ALPHABET) for _ in range(CLAIM_CODE_LENGTH))


async def _unique_claim_code(db: AsyncSession, organization_id: UUID) -> str:
    """org 내에서 아직 쓰이지 않는 인수 코드 (partial unique 와 경합 시 재시도)."""
    for _ in range(10):
        code = generate_claim_code()
        taken = await db.scalar(
            select(User.id).where(
                User.organization_id == organization_id, User.claim_code == code
            )
        )
        if taken is None:
            return code
    raise BadRequestError("Could not allocate a claim code — please retry")


async def _unique_username(db: AsyncSession) -> str:
    """유령용 자동 username — 전역 unique(uq_user_username). 운영자는 입력하지 않는다."""
    for _ in range(10):
        candidate = f"prov_{uuid.uuid4().hex[:10]}"
        exists = await user_repository.exists(db, {"username": candidate})
        if not exists:
            return candidate
    raise BadRequestError("Could not allocate a username — please retry")


async def create_provisional_user(
    db: AsyncSession,
    organization_id: UUID,
    *,
    full_name: str,
    role_id: UUID,
    store_ids: Sequence[UUID] = (),
    department: str | None = None,
    hourly_rate: float | None = None,
    caller: User | None = None,
    commit: bool = True,
) -> User:
    """미가입 직원 1명 생성 — 이름·역할·매장만으로. username/비밀번호는 자동.

    - is_active=False, is_provisional=True (fail-closed)
    - clockin_pin 미발급 (PIN 출근 2차 차단)
    - email=None — 본인이 인수할 때 채운다
    - crewid 는 기존 next_crewid, 매장 배정은 ensure_member_store 로 empid 자동 부여
    """
    name = (full_name or "").strip()
    if not name:
        raise BadRequestError("Name is required")

    role: Role | None = await role_repository.get_by_id(db, role_id, organization_id)
    if role is None:
        raise NotFoundError("Role not found")
    # 생성 권한 — create_user 와 동일 규칙 (자기보다 상위 역할 생성 금지)
    if caller is not None and caller.role:
        if role.priority < caller.role.priority:
            raise ForbiddenError("Cannot create a user with a role above your priority")
        if (
            role.priority == caller.role.priority
            and caller.role.priority != SUPER_OWNER_PRIORITY
        ):
            raise ForbiddenError("Cannot create a user with a role at your priority")

    username = await _unique_username(db)
    claim_code = await _unique_claim_code(db, organization_id)
    # password_hash 는 NOT NULL 이고 admin_login 이 is_active 검사보다 먼저 비밀번호를
    # 검증하므로(malformed 해시면 예외) 정상 형식의 해시를 넣되 값은 아무도 모르게 둔다.
    unusable_password = hash_password(secrets.token_urlsafe(32))

    try:
        user: User = await user_repository.create(
            db,
            {
                "organization_id": organization_id,
                "role_id": role_id,
                "username": username,
                "full_name": name,
                "email": None,
                "password_hash": unusable_password,
                "is_active": False,
                "is_provisional": True,
                "claim_code": claim_code,
                **({"department": department} if department is not None else {}),
                **({"hourly_rate": hourly_rate} if hourly_rate is not None else {}),
            },
        )

        from app.services.org_numbering import ensure_member_store, next_crewid

        db.add(
            OrgMember(
                user_id=user.id,
                organization_id=organization_id,
                role_id=role_id,
                hourly_rate=hourly_rate,
                department=department,
                clockin_pin=None,  # 유령은 PIN 없음
                employee_no=None,
                status="active",
                crewid=await next_crewid(db, organization_id),
            )
        )
        await db.flush()

        # 매장 배정 — empid 는 ensure_member_store 가 채번(그룹/번호대 정책 그대로 적용)
        from app.models.user_store import UserStore

        for store_id in store_ids:
            db.add(
                UserStore(
                    user_id=user.id,
                    store_id=store_id,
                    is_manager=False,
                    is_work_assignment=True,
                )
            )
            await ensure_member_store(db, user.id, store_id, is_work_assignment=True)
        # Owner 이상은 전 매장 자동 배정 (create_user 와 동일)
        if role.priority <= OWNER_PRIORITY:
            await user_repository.bulk_assign_org_stores_to_user(
                db, user.id, organization_id
            )
        await db.flush()

        if commit:
            await db.commit()
            await db.refresh(user)
        return user
    except Exception:
        if commit:
            await db.rollback()
        raise


async def create_provisional_users_bulk(
    db: AsyncSession,
    organization_id: UUID,
    people: list[dict],
    *,
    caller: User | None = None,
) -> list[User]:
    """미가입 직원 여러 명 생성 — 임포트/스케줄 화면에서 다건 생성용. 단일 트랜잭션.

    people: [{full_name, role_id, store_ids?, department?, hourly_rate?}]
    """
    created: list[User] = []
    try:
        for p in people:
            created.append(
                await create_provisional_user(
                    db,
                    organization_id,
                    full_name=p["full_name"],
                    role_id=p["role_id"],
                    store_ids=p.get("store_ids") or (),
                    department=p.get("department"),
                    hourly_rate=p.get("hourly_rate"),
                    caller=caller,
                    commit=False,  # 전체를 한 트랜잭션으로
                )
            )
        await db.commit()
        for u in created:
            await db.refresh(u)
        return created
    except Exception:
        await db.rollback()
        raise


async def regenerate_claim_code(
    db: AsyncSession, organization_id: UUID, user_id: UUID
) -> str:
    """인수 코드 재발급 — 코드를 잃어버렸거나 유출됐을 때."""
    user = await user_repository.get_by_id(db, user_id, organization_id)
    if user is None:
        raise NotFoundError("User not found")
    if not user.is_provisional:
        raise BadRequestError("Only provisional accounts have a claim code")
    try:
        user.claim_code = await _unique_claim_code(db, organization_id)
        await db.commit()
        return user.claim_code
    except Exception:
        await db.rollback()
        raise
