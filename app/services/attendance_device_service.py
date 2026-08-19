"""Attendance Device 서비스 — 매장 공용 기기 등록 + PIN 기반 clock in/out.

Attendance Device service layer — Handles terminal registration, token
verification, store assignment, and PIN-based clock operations that a
shared store device performs on behalf of any staff member.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import string
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.attendance import Attendance
from app.models.attendance_break import (
    VALID_BREAK_TYPES,
    PAID_BREAK_TYPES,
    UNPAID_BREAK_TYPES,
    AttendanceBreak,
    normalize_break_type,
)
from app.models.attendance_device import AttendanceDevice
from app.models.organization import Store
from app.models.user import User
from app.models.user_store import UserStore
from app.repositories.attendance_repository import attendance_repository
from app.utils.exceptions import BadRequestError, NotFoundError, UnauthorizedError
from app.utils.timezone import minutes_between, resolve_schedule_instants

# clock action 타입
ClockAction = Literal["clock_in", "break_start", "break_end", "clock_out"]

# device_name 에 사용할 영숫자 (혼동 문자 제외)
_NAME_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@dataclass
class IdentifyContext:
    """identify_user_by_pin 의 typed 반환값.

    today_status / current_break / scheduled_end 는 device 가 store 미할당이거나
    오늘 attendance 가 없으면 None. (primary attendance 기준)
    today_attendances 는 오늘 모든 attendance(=schedule) 목록 (Issue 8 다중 schedule).

    default_schedule_id 는 "앱이 기본으로 다룰 shift"(D13/D14). clock-in 대상이라는
    뜻이 아니다 — 활성 shift 가 있으면 그것이 기본이고, 그 shift 는 clock-in 불가다.
    server_time 은 **판정용이 아니라 신선도용**이다. picker 를 열어둔 채 시간이 흐르면
    프리뷰가 낡으므로 앱이 이 값 기준으로 재조회를 판단한다. 판정은 언제나 서버가 한다.
    """
    user: User
    today_status: str | None = None
    current_break: dict | None = None  # {break_type, started_at}
    scheduled_end: datetime | None = None
    today_attendances: list[dict] = field(default_factory=list)
    stale_attendances: list[dict] = field(default_factory=list)  # Issue 11
    default_schedule_id: UUID | None = None
    server_time: datetime | None = None


def generate_device_token() -> str:
    """URL-safe 32바이트 랜덤 토큰 (무기한). 기기에 1회만 반환, DB 에는 해시."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """sha256 hex digest — DB 저장용."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_device_name(suffix_length: int = 4) -> str:
    """기본 기기 이름 생성. 예: 'Terminal-A7K3'."""
    suffix = "".join(secrets.choice(_NAME_ALPHABET) for _ in range(suffix_length))
    return f"Terminal-{suffix}"


def shift_moment_label(value: datetime | None, tz) -> str | None:
    """시프트 시각을 **날짜와 함께** 사람이 읽는 라벨로 (예: "Aug 19, 5:00 PM").

    시각만 찍으면(`"5:00 PM"`) 하루 어긋난 스케줄이 문구상 완벽히 정상으로 보인다 —
    2026-08 오염 사고에서 "1439분 조기출근" 알림이 `5:00 PM` 하나만 달고 나가는 바람에
    받는 사람이 무엇이 이상한지 알 수 없었다. 조기/지각 문구가 날짜를 달고 있었으면
    그날 바로 드러났다. 조기 clock-in 의 세 채널(400 응답 / in-app / email)이 이 하나를
    같이 쓴다 — 채널마다 포맷이 갈리면 같은 사건이 다르게 읽힌다.
    """
    if value is None:
        return None
    return value.astimezone(tz).strftime("%b %-d, %-I:%M %p")


def generate_clockin_pin() -> str:
    """6자리 숫자 PIN 생성 (random, uniqueness 미보장).

    호출자가 commit 시 IntegrityError 처리해야 함 — `commit_pin_or_409` 사용.
    충돌 확률 1/1,000,000 이라 단일 호출 시 거의 발생 안 함.
    Bulk 케이스(마이그레이션 등) 에선 set 채우기 방식으로 사전 회피.
    """
    return f"{secrets.randbelow(1_000_000):06d}"


async def generate_unique_clockin_pin(
    db: AsyncSession,
    organization_id: UUID,
    exclude_user_id: UUID | None = None,
    attempts: int = 10,
) -> str:
    """org 안에서 중복이 없는 6자리 PIN 생성.

    `generate_clockin_pin()` 은 uniqueness 를 보장하지 않으므로 충돌 시 재시도한다.
    `attempts` 회 모두 실패하면 마지막 후보를 그대로 반환 — 최종 방어는
    unique 제약(`commit_pin_or_409`) 이다. org 당 PIN 수를 감안하면 실제로는
    1회차에 거의 끝난다.
    """
    from fastapi import HTTPException

    pin = generate_clockin_pin()
    for _ in range(attempts):
        try:
            await assert_no_pin_conflict(db, organization_id, pin, exclude_user_id)
            return pin
        except HTTPException:
            pin = generate_clockin_pin()
    return pin


async def suggest_available_clockin_pin(
    db: AsyncSession,
    organization_id: UUID,
    length: int = 4,
    exclude_user_id: UUID | None = None,
) -> str | None:
    """org 안에서 바로 쓸 수 있는(충돌 없는) `length` 자리 PIN 하나. 없으면 None.

    `generate_unique_clockin_pin` 은 6자리 고정 + 배정용이라 "관리자가 짧은 PIN 을
    직접 고르려고 빈 번호를 찾는" 용도로는 맞지 않는다. 여기서는 org 의 PIN 을 한 번만
    읽어와 메모리에서 후보를 검사한다 — 4자리 공간(10,000)이 작아서 랜덤 재시도보다
    확정적이고, 공간이 꽉 찼을 때 None 을 정확히 돌려줄 수 있다.

    후보 순서는 랜덤 시작점부터 순회 — 연속 호출이 늘 같은 번호를 주지 않게.
    """
    if not 4 <= length <= 6:
        raise ValueError("length must be between 4 and 6")

    stmt = select(User.clockin_pin).where(
        User.organization_id == organization_id,
        User.clockin_pin.isnot(None),
    )
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    existing_set: set[str] = {p for p in (await db.execute(stmt)).scalars().all() if p}

    space = 10**length
    start = secrets.randbelow(space)
    for offset in range(space):
        candidate = f"{(start + offset) % space:0{length}d}"
        if candidate in existing_set:
            continue
        return candidate
    return None


async def find_pin_conflicts(
    db: AsyncSession,
    organization_id: UUID,
    pin: str,
    exclude_user_id: UUID | None = None,
) -> list[tuple[UUID, str]]:
    """org 안에서 `pin` 을 이미 쓰고 있는 (user_id, clockin_pin) 목록. 없으면 빈 리스트.

    충돌 = **정확히 같은 값** 하나뿐이다. 길이가 다르면 서로 다른 PIN 이며,
    앞자리가 겹쳐도(`4885` / `488528`) 둘 다 쓸 수 있다 — 키오스크 식별이
    정확 일치(`clockin_pin == pin`)라 짧은 쪽이 긴 쪽을 가로채지 않는다.

    조회 전용이라 예외를 던지지 않는다. 배정 가능 여부만 알고 싶은 콘솔 lookup 과
    "막아야 하는가" 를 판정하는 assert 가 같은 판정을 보게 하려고 분리했다 —
    한쪽만 고치면 "available 이라 했는데 저장은 409" 가 난다.
    """
    stmt = select(User.id, User.clockin_pin).where(
        User.organization_id == organization_id,
        User.clockin_pin == pin,
    )
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)

    rows = (await db.execute(stmt)).all()
    return [(row[0], row[1]) for row in rows]


async def assert_no_pin_conflict(
    db: AsyncSession,
    organization_id: UUID,
    pin: str,
    exclude_user_id: UUID | None = None,
    store_id: UUID | None = None,
) -> None:
    """org 안에서 PIN 중복을 막는다. 이미 쓰는 사람이 있으면 409 (구조화 detail).

    충돌은 **정확히 같은 값** 뿐이다. 앞자리가 겹치는 다른 길이의 PIN(`4885` /
    `488528`)은 서로 다른 PIN 으로 공존한다 — 키오스크 식별이 정확 일치라
    짧은 쪽이 긴 쪽을 가로채지 않는다. (2026-08-13 결정: 이전의 prefix 금지
    규칙 제거. 남는 위험은 "6자리 직원이 4자리에서 멈추고 확인" 뿐인데,
    그 뒤 이름 확인 다이얼로그가 한 번 더 막는다.)

    선 검사로 친절한 409 를 주고, 동시성으로 빠져나간 경우는
    `commit_pin_or_409` 의 unique 위반이 최종 방어다.

    409 detail 계약 (모든 클라이언트 공통):
        {"code": "pin_conflict", "reason": "exact",
         "other_store": true|false|null, "message": "<영어 사유 문장>"}

    - reason: 항상 exact (prefix 값은 2026-08-13 이후 발생하지 않는다).
    - other_store: manage(키오스크) 경로에서만 채움 — `store_id` 가 주어지면
      충돌 유저가 그 매장(user_stores)에 없을 때 True. 그 외 경로는 None.
    - 타인의 PIN 값·이름은 어떤 필드에도 절대 싣지 않는다.
    """
    from fastapi import HTTPException, status

    conflicts = await find_pin_conflicts(db, organization_id, pin, exclude_user_id)
    if not conflicts:
        return

    conflict_user_id, _conflict_pin = conflicts[0]

    other_store: bool | None = None
    if store_id is not None:
        in_store = (
            await db.execute(
                select(UserStore.user_id)
                .where(
                    UserStore.user_id == conflict_user_id,
                    UserStore.store_id == store_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        other_store = in_store is None

    message = (
        "This PIN is already in use by an employee at another store."
        if other_store is True
        else "This PIN is already in use by another employee."
    )

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "pin_conflict",
            "reason": "exact",
            "other_store": other_store,
            "message": message,
        },
    )


async def commit_pin_or_409(db: AsyncSession) -> None:
    """commit. `uq_user_org_clockin_pin` 위반 시 409 pin_conflict 로 변환.

    unique 제약 fallback 이라 old-row 정보가 없다 — reason 은 exact 로 고정,
    other_store 는 null. 그 외 IntegrityError 는 그대로 raise.
    """
    from fastapi import HTTPException, status
    from sqlalchemy.exc import IntegrityError

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if "uq_user_org_clockin_pin" in str(exc.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "pin_conflict",
                    "reason": "exact",
                    "other_store": None,
                    "message": "This PIN is already in use by another employee.",
                },
            ) from exc
        raise


class AttendanceDeviceService:
    """attendance device 비즈니스 로직 모음."""

    # ── 등록 / 조회 / 해제 ─────────────────────────────────

    async def register(
        self,
        db: AsyncSession,
        organization_id: UUID,
        fingerprint: str | None = None,
    ) -> tuple[AttendanceDevice, str]:
        """새 기기 등록 — 랜덤 이름/토큰 발급. 평문 token 은 이 호출에서만 반환."""
        token = generate_device_token()
        device = AttendanceDevice(
            organization_id=organization_id,
            store_id=None,
            device_name=generate_device_name(),
            token_hash=hash_token(token),
            fingerprint=fingerprint,
            registered_at=datetime.now(timezone.utc),
        )
        db.add(device)
        await db.flush()
        return device, token

    async def get_by_token(
        self, db: AsyncSession, token: str
    ) -> AttendanceDevice | None:
        """평문 토큰 → 기기 조회 (revoke 시 row 삭제되므로 추가 필터 불필요)."""
        token_hash = hash_token(token)
        result = await db.execute(
            select(AttendanceDevice).where(
                AttendanceDevice.token_hash == token_hash,
            )
        )
        return result.scalar_one_or_none()

    async def touch_last_seen(self, db: AsyncSession, device: AttendanceDevice) -> None:
        """매 인증 성공 시 last_seen_at 갱신."""
        device.last_seen_at = datetime.now(timezone.utc)
        await db.flush()

    async def assign_store(
        self, db: AsyncSession, device: AttendanceDevice, store_id: UUID
    ) -> AttendanceDevice:
        """기기에 매장 할당/변경. 동일 조직 내 매장이어야 함.

        매장이 할당되면 `device_name` 을 store code (또는 name 앞 두 글자) 기반의
        순번 이름으로 재설정한다. 예: store.code='NB' → 'NB001', 'NB002'. code 가
        없으면 store.name 앞 두 글자 대문자 사용 (Hollywood → 'HO001').
        """
        from sqlalchemy import func as _func

        result = await db.execute(
            select(Store).where(
                Store.id == store_id,
                Store.organization_id == device.organization_id,
            )
        )
        store = result.scalar_one_or_none()
        if store is None:
            raise NotFoundError("Store not found in this organization")
        device.store_id = store_id

        # prefix 결정: store.code 우선, 없으면 store.name 앞 두 글자 대문자
        prefix: str | None = None
        if store.code:
            prefix = store.code.strip() or None
        if not prefix:
            base = (store.name or "").strip()
            if len(base) >= 2:
                prefix = base[:2].upper()
            elif len(base) == 1:
                prefix = base.upper()
        # 매장명이 공백이거나 비어있으면 fallback — 기존 이름 유지
        if prefix:
            # 같은 store 의 기기 수 (자기 자신 제외)
            count_stmt = (
                select(_func.count(AttendanceDevice.id))
                .where(
                    AttendanceDevice.store_id == store_id,
                    AttendanceDevice.id != device.id,
                )
            )
            count = (await db.execute(count_stmt)).scalar_one() or 0
            device.device_name = f"{prefix}{(count + 1):03d}"
        await db.flush()
        return device

    async def rename(
        self, db: AsyncSession, device: AttendanceDevice, new_name: str
    ) -> AttendanceDevice:
        name = (new_name or "").strip()
        if not name:
            raise BadRequestError("device_name is required")
        if len(name) > 100:
            raise BadRequestError("device_name too long (max 100)")
        device.device_name = name
        await db.flush()
        return device

    async def revoke(self, db: AsyncSession, device: AttendanceDevice) -> None:
        """해제 — row 즉시 삭제. 감사 이력 보존 안 함."""
        await db.delete(device)
        await db.flush()

    async def list_for_org(
        self,
        db: AsyncSession,
        organization_id: UUID,
    ) -> list[AttendanceDevice]:
        stmt = (
            select(AttendanceDevice)
            .where(AttendanceDevice.organization_id == organization_id)
            .order_by(AttendanceDevice.registered_at.desc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_manage(
        self,
        db: AsyncSession,
        organization_id: UUID,
        device_id: UUID,
    ) -> AttendanceDevice:
        result = await db.execute(
            select(AttendanceDevice).where(
                AttendanceDevice.id == device_id,
                AttendanceDevice.organization_id == organization_id,
            )
        )
        device = result.scalar_one_or_none()
        if device is None:
            raise NotFoundError("Device not found")
        return device

    # ── User + PIN 검증 ────────────────────────────────────

    async def _get_active_user(
        self, db: AsyncSession, user_id: UUID, organization_id: UUID
    ) -> User:
        """PIN 검증 없이 active user 조회 (manage override 전용)."""
        result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(
                User.id == user_id,
                User.organization_id == organization_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise BadRequestError("User not found")
        return user

    async def perform_clock_action_manage(
        self,
        db: AsyncSession,
        device: AttendanceDevice,
        action: ClockAction,
        user_id: UUID,
        manager_user_id: UUID,
        break_type: str | None = None,
        reason: str | None = None,
        schedule_id: UUID | None = None,
    ) -> Attendance:
        """매니저가 manage 모드에서 임의 사용자 attendance 를 처리.

        PIN 우회. early clock-in/out 가드 우회. note 에 manager 표시.

        `schedule_id` 는 겹침(D15) 상태에서 **어느 shift 를 처리하는지** 지목한다.
        미지정이면 기존처럼 첫 번째 열린 row 가 대상이다.
        """
        return await self.perform_clock_action(
            db,
            device=device,
            pin="",  # ignored
            action=action,
            user_id=user_id,
            break_type=break_type,
            reason=reason,
            skip_pin_check=True,
            skip_early_guards=True,
            manager_user_id=manager_user_id,
            schedule_id=schedule_id,
        )

    async def verify_user_pin(
        self, db: AsyncSession, user_id: UUID, pin: str, organization_id: UUID
    ) -> User:
        """user_id 로 유저를 조회 후 PIN 이 일치하는지 확인.

        기존 PIN → user 매핑 대신 user + PIN 검증 방식. 유저 없음/PIN 불일치
        모두 400 (device token 은 유효하므로 401 로 반환하지 않는다).
        """
        if not pin or not pin.isdigit() or not (4 <= len(pin) <= 6):
            raise BadRequestError("PIN must be 4-6 digits")
        result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(
                User.id == user_id,
                User.organization_id == organization_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            # 401 은 device token 문제에만 쓰고, 유저/PIN 오류는 400.
            raise BadRequestError("User not found")
        if user.clockin_pin != pin:
            raise BadRequestError("Invalid PIN")
        return user

    # ── 매장 Manager/SV 명단 (조기 출근 사유의 "누가 불렀나") ──────────────
    #
    # 목록 API 와 clock-in 의 `early_clock_in_requested_by` 검증이 **같은 규칙**을 써야
    # 한다. 규칙이 갈리면 "앱이 방금 받은 명단에서 골랐는데 서버가 거부" 가 난다.
    # 그래서 산출 규칙은 아래 `_store_manager_query()` 한 곳에만 있다.

    def _store_manager_query(
        self,
        *,
        organization_id: UUID,
        store_id: UUID,
        exclude_user_id: UUID | None = None,
    ):
        """조기 출근을 요청했을 법한 사람 = 해당 매장의 Manager/SV.

        포함 규칙 (계약 §4):
          - `SUPER_OWNER_PRIORITY < priority <= SV_PRIORITY` — Super Owner 는 매장 운영을
            하지 않으므로 제외, Owner/GM/SV 는 포함.
          - active + 미삭제. 비활성/퇴사자는 부를 수 없는 사람이다.
          - Owner 는 `user_stores` 행 없이도 전 매장 관리로 취급하는 기존 규칙을 따른다.
            그 외에는 이 매장에 소속(`user_stores`)돼 있어야 한다.
            `is_manager=True` 로 더 좁히지 않는 이유: 실제로 부르는 일이 잦은
            `is_work_assignment` SV 가 빠져 "직접 입력" 으로 새어나가 집계가 비어버린다.
          - 본인 제외 — "누가 불렀나" 에 자기 자신은 답이 아니다.
        """
        from app.core.permissions import SUPER_OWNER_PRIORITY, SV_PRIORITY, OWNER_PRIORITY
        from app.models.user import Role

        assigned_here = (
            select(UserStore.id)
            .where(UserStore.user_id == User.id, UserStore.store_id == store_id)
            .exists()
        )
        query = (
            select(User)
            .join(Role, Role.id == User.role_id)
            .options(selectinload(User.role))
            .where(
                User.organization_id == organization_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
                Role.priority > SUPER_OWNER_PRIORITY,
                Role.priority <= SV_PRIORITY,
                or_(Role.priority <= OWNER_PRIORITY, assigned_here),
            )
            .order_by(Role.priority.asc(), User.full_name.asc())
        )
        if exclude_user_id is not None:
            query = query.where(User.id != exclude_user_id)
        return query

    async def list_store_managers(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        store_id: UUID,
        asking_user_id: UUID,
    ) -> list[dict]:
        """매장 Manager/SV 명단 — 조기 출근 사유 단계에서만 조회한다.

        빈 목록도 정상이다(200 []). 앱은 항상 "Someone else" 로 계속 진행할 수 있다.
        """
        from app.utils.names import display_name

        result = await db.execute(
            self._store_manager_query(
                organization_id=organization_id,
                store_id=store_id,
                exclude_user_id=asking_user_id,
            )
        )
        # 표시할 이름이 없는 계정은 **아예 내리지 않는다.**
        # username 으로 폴백하면 키오스크 화면에 로그인 계정명(`admin` 등)이 뜬다 —
        # 이 엔드포인트가 PIN 게이트까지 두는 이유(매니저 명단 = 사회공학 표적)와
        # 정면으로 어긋난다. 이름이 없으면 직원이 그 사람을 알아보지도 못하므로
        # 목록에 둘 값어치도 없다. 명단이 비어도 "직접 입력" 으로 계속 진행할 수 있다.
        rows: list[dict] = []
        for u in result.scalars().all():
            name = (display_name(u) or "").strip()
            if not name:
                continue
            rows.append(
                {
                    "user_id": u.id,
                    "full_name": name,
                    "role_name": u.role.name if u.role else "",
                    "role_priority": u.role.priority if u.role else 999,
                }
            )
        return rows

    async def _is_valid_reason_user(
        self,
        db: AsyncSession,
        *,
        candidate_id: UUID,
        organization_id: UUID,
        store_id: UUID,
        asking_user_id: UUID,
    ) -> bool:
        """`early_clock_in_requested_by` 가 명단 규칙을 통과하는지.

        목록(`list_store_managers`)과 **같은 규칙**이어야 한다 — 이름 없는 계정은
        목록에 없으므로 id 로 지목하는 것도 받지 않는다.
        """
        from app.utils.names import display_name

        found = await db.scalar(
            self._store_manager_query(
                organization_id=organization_id,
                store_id=store_id,
                exclude_user_id=asking_user_id,
            ).where(User.id == candidate_id)
        )
        if found is None:
            return False
        return bool((display_name(found) or "").strip())

    async def identify_manager_by_pin(
        self,
        db: AsyncSession,
        organization_id: UUID,
        pin: str,
    ) -> User:
        """매니저 진입용: PIN 으로 organization 안 active user 식별.

        identify_user_by_pin 과 비슷하지만 attendance context 계산 없이 User 만 반환.
        매니저 자격(SV+) 검증은 호출자가 수행.
        """
        if not pin or not pin.isdigit() or not (4 <= len(pin) <= 6):
            raise BadRequestError("PIN must be 4-6 digits")
        result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(
                User.organization_id == organization_id,
                User.clockin_pin == pin,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise BadRequestError("Invalid PIN")
        return user

    async def identify_user_by_pin(
        self,
        db: AsyncSession,
        pin: str,
        device: AttendanceDevice,
    ) -> "IdentifyContext":
        """PIN 단독으로 device 의 org 내 user 식별 + 오늘 attendance context 반환.

        PIN-first 키오스크 흐름 entry point (Phase 3 + Stage J 확장). 직원이 PIN
        입력하면 본인 식별 + 오늘 스케줄 있으면 today_status / current_break /
        scheduled_end 반환. 스케줄 없으면 셋 다 None.

        verify_user_pin 과 달리 user_id 필요 없음 — `(organization_id, clockin_pin)` unique
        제약 (Phase 1) 으로 단일 row 식별 가능.

        매니저 권한 / manage 모드 진입 검증은 본 endpoint 에 포함하지 않음 (Phase 6 에서 별도).
        """
        # 1. PIN 형식 — Stage J 부터 4~6자리 가변.
        if not pin or not pin.isdigit() or not (4 <= len(pin) <= 6):
            raise BadRequestError("PIN must be 4-6 digits")

        # 2. user 조회 — org/active/non-deleted, PIN 일치
        result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(
                User.organization_id == device.organization_id,
                User.clockin_pin == pin,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise BadRequestError("Invalid PIN")

        # 3. context 계산 — device 에 store 없으면 today_status / current_break /
        #    scheduled_end 모두 None.
        if device.store_id is None:
            return IdentifyContext(user=user)

        return await self._compute_identify_context_for_user(
            db,
            user=user,
            store_id=device.store_id,
            organization_id=device.organization_id,
        )

    async def _compute_identify_context_for_user(
        self,
        db: AsyncSession,
        user: User,
        store_id: UUID,
        organization_id: UUID,
    ) -> "IdentifyContext":
        """store work_date 기준 오늘 user 의 attendance context 계산.

        (Issue 8) 한 직원이 같은 날 2+ schedule 을 가질 수 있으므로 모든 row 를 가져와
        우선순위로 정렬한 list 를 반환. primary (정렬 첫 번째) 가 today_status 등 단일
        필드 채움 (단일 schedule 케이스 호환).

        반환 dataclass 의 필드:
          - today_status: primary attendance 의 effective status. 스케줄 없으면 None.
          - current_break: primary 가 on_break 일 때 (break_type, started_at) dict.
          - scheduled_end: primary schedule end_time → store TZ aware UTC.
          - today_attendances: 오늘 모든 attendance dict 목록 (우선순위 정렬).
        """
        from zoneinfo import ZoneInfo

        from app.models.schedule import Schedule
        from app.services.attendance_service import compute_effective_status
        from app.services.attendance_threshold_service import resolve_thresholds
        from app.utils.timezone import get_store_day_config, get_work_date

        now = datetime.now(timezone.utc)
        store_tz, store_day_start = await get_store_day_config(db, store_id)
        today = get_work_date(store_tz, store_day_start, now)
        tz_info = ZoneInfo(store_tz)

        def _tz_hhmm(value: datetime | None) -> str | None:
            return value.astimezone(tz_info).strftime("%H:%M") if value else None

        # (Issue 11) 이전 work_date 미완료(orphan) — 오늘 attendance 유무와 무관하게
        # 항상 조회 (오늘 schedule 없어도 어제 미완료 있으면 경고). 최근 30일, 기기 매장.
        from datetime import timedelta as _td_stale
        stale_rows = list(
            (
                await db.execute(
                    select(
                        Attendance.id,
                        Attendance.work_date,
                        Attendance.status,
                        Attendance.clock_in,
                    )
                    .where(
                        Attendance.user_id == user.id,
                        Attendance.store_id == store_id,
                        Attendance.clock_in.isnot(None),
                        Attendance.clock_out.is_(None),
                        Attendance.status.in_(["working", "on_break", "late"]),
                        Attendance.work_date < today,
                        Attendance.work_date >= today - _td_stale(days=30),
                    )
                    .order_by(Attendance.work_date.desc())
                )
            ).all()
        )
        # 어제 영업일의 **진행 중** 야간조는 아래 후보 목록(today_attendances)에 그대로
        # 실려 Clock Out 을 제시한다 — 그 경우 여기서 다시 "지난 근무가 안 닫혔다"
        # 경고까지 띄우면, 화면이 바로 옆에서 해결책을 주고 있는데도 사고처럼 보인다.
        # 목록에 실린 attendance 는 아래에서 제외한다(`_prune_stale`).
        def _stale_items(exclude_ids: set[UUID]) -> list[dict]:
            return [
                {"work_date": wd, "status": st, "clock_in_display": _tz_hhmm(ci)}
                for (att_id, wd, st, ci) in stale_rows
                if att_id not in exclude_ids
            ]

        stale = _stale_items(set())

        from datetime import timedelta as _td_prev_day

        from app.utils.attendance_judgement import ClockInKind, judge_clock_in
        from app.utils.attendance_overlap import overlapping_keys_from_rows
        from app.utils.attendance_shift_candidates import (
            ShiftCandidate,
            clock_in_eligibility,
            is_open_prev_day_candidate,
            is_within_operating_window,
            pick_default_shift,
            sort_shift_candidates,
        )
        from app.utils.timezone import operating_day_window

        yesterday = today - _td_prev_day(days=1)
        # 영업일 창(오늘·어제) — clock-in 경로와 **같은 값, 같은 규칙**으로 후보를 거른다.
        # 목록에서만 걸러지고 clock-in 에서 안 걸러지면(또는 반대면) 화면과 서버가 갈린다.
        day_windows = {
            d: operating_day_window(store_tz, store_day_start, d)
            for d in (today, yesterday)
        }

        rows = list(
            (
                await db.execute(
                    select(Attendance, Schedule)
                    .outerjoin(Schedule, Schedule.id == Attendance.schedule_id)
                    .where(
                        Attendance.user_id == user.id,
                        Attendance.store_id == store_id,
                        Attendance.work_date == today,
                        Attendance.status != "cancelled",
                    )
                )
            ).all()
        )
        seen_schedule_ids = {
            att.schedule_id for att, _sch in rows if att.schedule_id is not None
        }

        # clock-in 후보 조회(Schedule 기준)와 목록(Attendance 기준)의 **출처를 맞춘다**.
        # 두 쿼리가 갈리면 (a) eager 훅이 누락한 스케줄은 picker 에 안 보이는데 서버는
        # 고를 수 있고, (b) 어제 영업일 야간조 shift 는 어느 쪽에도 안 나온다.
        sched_rows = list(
            (
                await db.execute(
                    select(Schedule, Attendance)
                    .outerjoin(Attendance, Attendance.schedule_id == Schedule.id)
                    .where(
                        Schedule.user_id == user.id,
                        Schedule.store_id == store_id,
                        Schedule.operating_day.in_([today, yesterday]),
                        Schedule.status == "confirmed",
                    )
                )
            ).all()
        )
        for schedule, att in sched_rows:
            if schedule.id in seen_schedule_ids:
                continue
            if att is not None and att.status == "cancelled":
                continue
            seen_schedule_ids.add(schedule.id)
            rows.append((att, schedule))

        if not rows:
            return IdentifyContext(user=user, stale_attendances=stale)

        thresholds = await resolve_thresholds(
            db, organization_id=organization_id, store_id=store_id
        )
        late_buffer = thresholds.late_buffer

        def _display(value: datetime | None) -> str | None:
            if value is None:
                return None
            return value.astimezone(tz_info).strftime("%H:%M")

        # 겹침 표시 — 앱이 "두 shift 에 출근해 있다" 배너를 해소될 때까지 띄우는 근거.
        overlap_ids = overlapping_keys_from_rows(
            [(att.id, att.clock_in, att.clock_out) for att, _s in rows if att is not None],
            now=now,
        )

        # 각 row → (후보, item dict). 후보는 정렬/기본선택 규칙(clock-in 경로와 공용)에,
        # item 은 응답에 쓴다.
        entries: list[tuple[ShiftCandidate, dict]] = []
        for att, schedule in rows:
            eff_status = compute_effective_status(
                att_status=att.status if att is not None else "upcoming",
                att_clock_in=att.clock_in if att is not None else None,
                schedule_start_time=schedule.start_time if schedule else None,
                schedule_end_time=schedule.end_time if schedule else None,
                schedule_work_date=schedule.work_date if schedule else None,
                now=now,
                store_tz=tz_info,
                late_buffer=late_buffer,
                schedule_start_at=schedule.start_at if schedule else None,
                schedule_end_at=schedule.end_at if schedule else None,
            )

            sched_start_utc: datetime | None = None
            sched_end_utc: datetime | None = None
            if schedule is not None:
                _ss, _se = resolve_schedule_instants(
                    start_at=schedule.start_at, end_at=schedule.end_at,
                    work_date=schedule.work_date, start_time=schedule.start_time,
                    end_time=schedule.end_time, tz_name=tz_info.key,
                )
                sched_start_utc = _ss.astimezone(timezone.utc) if _ss else None
                sched_end_utc = _se.astimezone(timezone.utc) if _se else None

            candidate = ShiftCandidate(
                schedule_id=schedule.id if schedule is not None else None,
                operating_day=(
                    schedule.operating_day if schedule is not None
                    else (att.work_date if att is not None else None)
                ),
                scheduled_start=sched_start_utc,
                scheduled_end=sched_end_utc,
                attendance_id=att.id if att is not None else None,
                attendance_status=att.status if att is not None else None,
                clock_in=att.clock_in if att is not None else None,
                clock_out=att.clock_out if att is not None else None,
            )
            # 어제 영업일 shift 를 목록에 남기는 조건은 두 갈래다.
            #  (a) **아직 진행 중**(찍고 안 닫음) — 무조건 남긴다. 매장 day_start 를
            #      넘겨 끝나는 야간조가 여기다. 이 row 를 빼면 today_status 가 null 이
            #      되어 앱이 Clock Out 을 아예 못 띄우고, Clock In 을 누르면 서버의
            #      열린-row 가드가 400 으로 막아 키오스크에서 퇴근이 불가능해진다.
            #  (b) 미출근 후보 — clock-in 경로와 **같은 조건**(종료 후 4시간 이내)일
            #      때만. 안 그러면 picker 가 보여준 shift 를 서버가 거부하는 화면이 된다.
            if (
                candidate.operating_day is not None
                and candidate.operating_day < today
                and not candidate.is_active
                and not is_open_prev_day_candidate(candidate, now=now)
            ):
                continue

            # 자기 영업일 창 밖에서 시작하는 **미출근** shift — 목록에는 남기고
            # `clock_in_eligible=false` + 이유로 내린다(아래 clock_in_eligibility).
            #
            # 예전엔 여기서 `continue` 로 아예 뺐다. 그러면 잘못 만들어진 스케줄이
            # 화면에서 **소리 없이 사라져** 결근인지 잘못 만든 건지 구분할 수 없고,
            # 매장에서는 "분명 넣었는데 아무 데도 안 보인다" 가 된다.
            # 고를 수 없게 하는 것과 안 보이게 하는 것은 다르다.
            in_window = (not candidate.is_open) or is_within_operating_window(
                candidate, windows=day_windows
            )

            cur_break: dict | None = None
            if eff_status == "on_break" and att is not None:
                br = (
                    await db.execute(
                        select(AttendanceBreak)
                        .where(
                            AttendanceBreak.attendance_id == att.id,
                            AttendanceBreak.ended_at.is_(None),
                        )
                        .order_by(AttendanceBreak.started_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if br is not None:
                    cur_break = {
                        "break_type": br.break_type,
                        "started_at": br.started_at,
                    }

            eligible, ineligible_reason = clock_in_eligibility(
                candidate, within_window=in_window
            )
            # 판정 프리뷰 — "지금 이 shift 로 찍으면 어떻게 기록되는가"(D3).
            # **숫자만** 내린다. "3h 12m late" 문자열은 앱이 l10n 으로 조립한다.
            # 계산은 clock-in 실기록과 똑같이 judge_clock_in 을 쓴다 — 앱이 자체 계산하면
            # 초 버림 지점이 하나 더 생기고 화면과 기록이 갈린다(D16).
            preview: dict | None = None
            if eligible:
                verdict = judge_clock_in(
                    now,
                    sched_start_utc,
                    late_buffer=thresholds.late_buffer,
                    early_threshold=thresholds.early_clock_in,
                )
                preview = {
                    "kind": verdict.kind.value,
                    "minutes_early": verdict.minutes_early,
                    "minutes_late": verdict.minutes_late,
                    "reason_required": verdict.kind is ClockInKind.EARLY,
                }

            entries.append((candidate, {
                "schedule_id": candidate.schedule_id,
                "attendance_id": candidate.attendance_id,
                "operating_day": candidate.operating_day,
                "status": eff_status,
                "scheduled_start": sched_start_utc,
                "scheduled_end": sched_end_utc,
                "scheduled_start_display": _display(sched_start_utc),
                "scheduled_end_display": _display(sched_end_utc),
                "clock_in": candidate.clock_in,
                "clock_in_display": _display(candidate.clock_in),
                "clock_in_eligible": eligible,
                "ineligible_reason": ineligible_reason,
                "is_default": False,
                "overlapping": candidate.attendance_id in overlap_ids,
                "clock_in_preview": preview,
                "current_break": cur_break,
            }))

        if not entries:
            return IdentifyContext(user=user, stale_attendances=stale)

        # 정렬 = 활성 → 미출근(오늘 시간순 → 어제) → 완료/불가 (§1.3). status 랭크
        # (upcoming 4 < no_show 5) 를 버린 이유가 원인 A 다 — 미출근 오전 shift 가
        # no_show 로 강등돼 저녁 shift 뒤로 밀리면서 앱이 저녁 id 를 실어 보냈다.
        #
        # `today=` 를 반드시 넘긴다. 이 목록은 오늘·어제가 섞여 있고, 어제 shift 의
        # 시작 시각은 오늘 것보다 항상 이르다 — 안 넘기면 어제 결근 shift 가 0번 +
        # default_schedule_id 를 가져가고, 구버전 HTMA 는 그 id 를 그대로 실어 보내
        # (명시 선택으로 수용된다) 오늘 근무가 통째로 어제 영업일에 귀속된다.
        order = {
            id(candidate): index
            for index, candidate in enumerate(
                sort_shift_candidates([c for c, _i in entries], today=today)
            )
        }
        entries.sort(key=lambda pair: order[id(pair[0])])

        # 기본 제시에서는 **고를 수 없는 shift 를 뺀다**. 창 밖 시프트는 목록에 남지만
        # (존재 자체는 보여야 하므로) 기본값으로 내밀면 앱이 그 id 를 실어 보내고
        # 서버가 거부하는, 현장에서 손쓸 수 없는 화면이 된다.
        # 이미 찍은 shift 는 계속 후보다 — clock-out 대상이 그쪽이기 때문.
        _selectable = [
            c for c, i in entries
            if i.get("clock_in_eligible") or i.get("clock_in") is not None
        ]
        default = pick_default_shift(_selectable or [c for c, _i in entries], today=today)
        default_schedule_id = default.schedule_id if default is not None else None
        items = [item for _c, item in entries]
        for candidate, item in entries:
            if default is not None and candidate is default:
                item["is_default"] = True

        primary = items[0]
        listed_ids = {
            item["attendance_id"] for item in items if item["attendance_id"] is not None
        }
        return IdentifyContext(
            user=user,
            today_status=primary["status"],
            current_break=primary["current_break"],
            scheduled_end=primary["scheduled_end"],
            today_attendances=items,
            stale_attendances=_stale_items(listed_ids),
            default_schedule_id=default_schedule_id,
            server_time=now,
        )

    # ── Clock 동작 ─────────────────────────────────────────

    async def _get_open_break(
        self, db: AsyncSession, attendance_id: UUID
    ) -> AttendanceBreak | None:
        """해당 attendance 의 아직 닫히지 않은 break 1건 조회."""
        result = await db.execute(
            select(AttendanceBreak)
            .where(
                AttendanceBreak.attendance_id == attendance_id,
                AttendanceBreak.ended_at.is_(None),
            )
            .order_by(AttendanceBreak.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def perform_clock_action(
        self,
        db: AsyncSession,
        device: AttendanceDevice,
        pin: str,
        action: ClockAction,
        user_id: UUID,
        break_type: str | None = None,
        reason: str | None = None,
        skip_pin_check: bool = False,
        skip_early_guards: bool = False,
        manager_user_id: UUID | None = None,
        schedule_id: UUID | None = None,
        walk_in: bool = False,
        allow_overlap: bool = False,
        early_clock_in_requested_by: UUID | None = None,
    ) -> Attendance:
        """기기 + user_id + PIN 으로 clock in/out/break 처리.

        break 는 attendance_breaks 테이블에 행 단위로 기록. break-start 는
        break_type 필수 (paid_10min | unpaid_meal). 같은 attendance 에 여러 번
        휴식 가능하며 open 상태 (ended_at IS NULL) 는 1건만 허용.

        Admin override 모드 (skip_pin_check=True) 는 매니저가 키오스크 관리자 모드에서
        타인 attendance 를 처리할 때 사용. PIN 우회 + early in/out 가드 우회.

        `schedule_id` 는 clock_in 뿐 아니라 clock_out/break 에서도 **대상 row 를 지목**한다
        (D15 겹침 이후엔 열린 row 가 둘일 수 있어 "첫 번째 열린 row" 규칙만으로는 어느
        shift 를 닫는지 아무도 모른다). 미지정이면 기존 `_active_row()` fallback 그대로.

        `allow_overlap` 은 "다른 shift 가 열려 있는 걸 알면서 또 찍는다" 는 직원의 확인이다.
        플래그 하나로 가드를 통째로 여는 게 아니라, 명시 `schedule_id` 가 열린 row 와 다른
        shift 를 가리킬 때만 연다 — 자동 선택(fallback)으로는 절대 열리지 않는다.
        키오스크 더블탭/네트워크 재시도가 조용히 두 row 를 만드는 것을 지금 이 가드
        하나가 막고 있기 때문이다.

        `early_clock_in_requested_by` 는 "누가 일찍 오라고 했나"(D9) 의 **식별자**다.
        표시용 문자열은 지금처럼 `reason` 으로 들어와 타임라인에 남고, 이 값은 별도
        컬럼에 저장된다. early override 가 성립할 때만 소비하며 그 외엔 조용히 무시한다
        (구버전 HTMA 는 아예 보내지 않는다).
        """
        if device.store_id is None:
            raise BadRequestError("Device has no store assigned")

        if skip_pin_check:
            user = await self._get_active_user(db, user_id, device.organization_id)
        else:
            user = await self.verify_user_pin(db, user_id, pin, device.organization_id)
        store_id = device.store_id
        now = datetime.now(timezone.utc)

        # 타임존/work_date 결정 — 매장 기준
        from app.utils.timezone import get_store_day_config, get_work_date

        store_tz, store_day_start = await get_store_day_config(db, store_id)
        today: date = get_work_date(store_tz, store_day_start, now)

        # Split shift 대응 — 하루 여러 row 가 있을 수 있으므로 list 로 조회.
        day_rows = await attendance_repository.list_user_day(db, user.id, today)
        # 새벽 근무(라벨=전날 영업일)가 경계(day_start)를 넘겨 아직 진행 중일 수 있다 —
        # break/clock-out 과 이중 clock-in 가드는 전날 라벨의 "열린"(clock_in 있고
        # clock_out 없는) row 도 봐야 한다. 안 그러면 01:00~09:00 새벽조가 06:00 경계를
        # 넘는 순간 퇴근/휴식이 전부 불가능해진다.
        from datetime import timedelta as _td_prev
        prev_rows = await attendance_repository.list_user_day(db, user.id, today - _td_prev(days=1))
        open_prev = [
            r for r in prev_rows
            if r.clock_in is not None and r.clock_out is None
        ]
        rows_ext = list(day_rows) + open_prev

        # clock-in 외 액션(break/clock_out)은 "지금 활성" row 기준.
        # working → on_break → late 순으로 찾고, 없으면 None.
        def _active_row() -> Attendance | None:
            # 겹침 허용(D15) 이후엔 열린 row 가 둘일 수 있다. 그때 "첫 번째 열린 row"
            # 규칙만 남겨두면 매니저가 어느 shift 를 퇴근시켰는지 알 수 없으므로,
            # 호출자가 schedule_id 로 지목하면 그것을 먼저 쓴다.
            if schedule_id is not None:
                for r in rows_ext:
                    if r.schedule_id == schedule_id and r.status in (
                        "working", "on_break", "late",
                    ):
                        return r
            # 미지정 fallback. `list_user_day()` 에는 ORDER BY 가 없어 DB 가 주는 순서가
            # 사실상 임의다 — 열린 row 가 둘일 때(D15) "어느 shift 를 닫는지 아무도
            # 모르는" 상태가 된다. 최소한 규칙은 하나로 못 박는다: **가장 최근에 찍은
            # 것부터**. 앱의 기본 제시(`pick_default_shift` 의 활성 정렬)와 같은 규칙이라
            # 화면이 가리킨 shift 와 실제로 닫히는 shift 가 어긋나지 않는다.
            ordered_rows = sorted(
                rows_ext,
                key=lambda r: (
                    0 if r.clock_in is not None else 1,
                    -(r.clock_in.timestamp() if r.clock_in is not None else 0.0),
                ),
            )
            for target_status in ("working", "on_break", "late"):
                for r in ordered_rows:
                    if r.status == target_status:
                        return r
            return None

        attendance = _active_row() if action != "clock_in" else (day_rows[0] if day_rows else None)

        # ── 타임라인용 before 캡처 ──
        # 각 분기가 실제로 건드리는 row 를 기준으로 변경 직전 값을 잡아둔다.
        # 여기서 안 잡으면 "이전 상태"를 영영 복원할 수 없다 (기존 버그의 원인).
        before_status: str | None = None
        before_clock_in: datetime | None = None
        before_clock_out: datetime | None = None
        touched_break: AttendanceBreak | None = None
        break_before: tuple[datetime | None, datetime | None, str | None] = (None, None, None)

        if action == "clock_in":
            # 1) clock-in 대상 shift 후보 — identify 와 **같은 순수 규칙**을 쓴다.
            #    두 경로가 각자 후보를 고르던 것이 원인 A 의 절반이었다
            #    (키오스크는 Schedule 기준 "가장 가까운 미래", identify 는 status 랭크).
            from app.models.schedule import Schedule
            from zoneinfo import ZoneInfo as _Zi

            from app.utils.attendance_shift_candidates import (
                ShiftCandidate,
                pick_fallback_shift,
                split_candidates,
            )
            from app.utils.timezone import operating_day_window

            yesterday = today - _td_prev(days=1)
            sch_result = await db.execute(
                select(Schedule)
                .where(
                    Schedule.user_id == user.id,
                    Schedule.store_id == store_id,
                    # (D4) 야간조 — 매장 day_start 경계를 넘겨 지각하면 어제 영업일 라벨의
                    # shift 로 찍어야 한다. 어제 것은 아래 split_candidates 가 "미출근 +
                    # 종료 후 4시간 이내" 로 다시 좁힌다.
                    Schedule.operating_day.in_([today, yesterday]),
                    Schedule.status == "confirmed",
                )
                .order_by(Schedule.start_at.asc().nulls_last())
            )
            all_schedules = list(sch_result.scalars().all())
            schedule_by_id = {s.id: s for s in all_schedules}
            today_schedules = [s for s in all_schedules if s.operating_day == today]

            tz = _Zi(store_tz)

            def _instants(s):
                return resolve_schedule_instants(
                    start_at=s.start_at, end_at=s.end_at, work_date=s.work_date,
                    start_time=s.start_time, end_time=s.end_time, tz_name=tz.key,
                )

            def _start_dt(s):
                return _instants(s)[0]

            def _end_dt(s):
                return _instants(s)[1]

            # 후보의 attendance 사실(찍었나/닫혔나)은 이미 읽어 둔 오늘/어제 row 에서 온다
            # — 후보 조회 때문에 쿼리를 더 늘리지 않는다.
            att_by_schedule = {
                r.schedule_id: r
                for r in (list(day_rows) + list(prev_rows))
                if r.schedule_id is not None
            }

            def _to_candidate(s) -> ShiftCandidate:
                att = att_by_schedule.get(s.id)
                return ShiftCandidate(
                    schedule_id=s.id,
                    operating_day=s.operating_day,
                    scheduled_start=_start_dt(s),
                    scheduled_end=_end_dt(s),
                    attendance_id=att.id if att is not None else None,
                    attendance_status=att.status if att is not None else None,
                    clock_in=att.clock_in if att is not None else None,
                    clock_out=att.clock_out if att is not None else None,
                )

            # 영업일 창은 **호출자가 주입**한다 — 후보 규칙 모듈은 DB 를 읽지 않는다.
            # identify 도 같은 창을 만들어 같은 규칙으로 거른다. 두 경로가 갈리면
            # "목록엔 보이는데 찍으면 거부" 가 된다.
            windows = {
                d: operating_day_window(store_tz, store_day_start, d)
                for d in (today, yesterday)
            }
            today_candidates, prev_candidates = split_candidates(
                [_to_candidate(s) for s in all_schedules],
                now=now,
                today=today,
                windows=windows,
            )
            candidates = today_candidates + prev_candidates

            # 2) 이중 clock-in 가드 — 실제로 출근중인 shift(clock_in 있고 clock_out 없는
            #    working/on_break/late) 가 있으면 원칙적으로 차단. late 는 "스케줄 지났는데
            #    미출근" 일 수 있어 clock_in 여부로 판단한다 (미출근 late 는 막지 않는다).
            #
            #    D15 예외: 직원이 **다른 shift 를 명시적으로 지목**하고 겹침을 확인
            #    (`allow_overlap`)했을 때만 연다. 자동 선택으로는 열리지 않는다.
            open_rows = [
                r for r in rows_ext
                if r.clock_in is not None and r.clock_out is None
                and r.status in ("working", "on_break", "late")
            ]
            overlap_open_rows: list[Attendance] = []
            if open_rows:
                open_schedule_ids = {r.schedule_id for r in open_rows}
                picks_other_shift = (
                    schedule_id is not None
                    and schedule_id not in open_schedule_ids
                    and any(c.schedule_id == schedule_id for c in candidates)
                )
                if not picks_other_shift:
                    # 같은 shift 재출근 / 자동 선택 / 후보 밖 지목 — 전부 기존 그대로 막는다.
                    # 구버전 HTMA 는 today_attendances[0](= 활성 shift) 를 보내므로 항상 여기.
                    raise BadRequestError("Previous shift not clocked out. Clock out first.")
                if not allow_overlap:
                    from app.core.error_codes.attendance import (
                        OVERLAPPING_CLOCK_IN_CONFIRMATION_REQUIRED,
                    )

                    first_open = open_rows[0]
                    open_sched = schedule_by_id.get(first_open.schedule_id)
                    o_start, o_end = (
                        _instants(open_sched) if open_sched is not None else (None, None)
                    )

                    def _hhmm(value: datetime | None) -> str | None:
                        return value.astimezone(tz).strftime("%H:%M") if value else None

                    raise OVERLAPPING_CLOCK_IN_CONFIRMATION_REQUIRED(
                        open_attendance_ids=[str(r.id) for r in open_rows],
                        open_schedule_ids=[
                            str(r.schedule_id) for r in open_rows if r.schedule_id
                        ],
                        open_scheduled_start_display=_hhmm(o_start),
                        open_scheduled_end_display=_hhmm(o_end),
                    )
                overlap_open_rows = list(open_rows)

            # 3) 오늘 후보가 없으면 워크인 경로. **어제 후보는 이 판단에 넣지 않는다** —
            #    어제 결근 shift 하나가 남아 있다고 워크인을 막으면 오늘의 근무가 통째로
            #    하루 전 영업일에 귀속되어 급여 기간이 밀린다. 단 직원이 어제 shift 를
            #    명시적으로 지목했다면 그건 사람의 판단이므로 워크인을 만들지 않는다.
            explicitly_picked = schedule_id is not None and any(
                c.schedule_id == schedule_id for c in candidates
            )
            if not today_candidates and not explicitly_picked:
                # 사용할 수 있는 "열린" 스케줄이 없음. 매장이 walk_in 을 허용하고 요청이
                # 워크인 의도이면, 오늘 스케줄이 아예 없든 / 이전 (워크인)shift 가 모두
                # clocked_out 이든 관계없이 **새 워크인 스케줄을 생성**한다. → 퇴근 후
                # 다시 출근(하루 여러 shift) 가능. (열린 shift 가 남아있으면 위 active 가드가
                # 먼저 막으므로, 여기 도달했다는 건 열린 shift 가 없다는 뜻.)
                from app.utils.settings_resolver import (
                    SettingNotRegisteredError as _SNRE_wi,
                    resolve_setting as _resolve_wi,
                )

                walk_in_allowed = False
                if walk_in:
                    try:
                        walk_in_allowed = bool(
                            await _resolve_wi(
                                db,
                                key="attendance.walk_in_allowed",
                                organization_id=device.organization_id,
                                store_id=store_id,
                            )
                        )
                    except _SNRE_wi:
                        walk_in_allowed = False

                if walk_in_allowed:
                    from app.services.schedule_service import schedule_service
                    walk_in_schedule = await schedule_service.create_walk_in_schedule(
                        db,
                        organization_id=device.organization_id,
                        store_id=store_id,
                        user_id=user.id,
                        work_date=today,
                        clock_in_at=now,
                        store_tz=store_tz,
                        created_by=user.id,
                    )
                    schedule_by_id[walk_in_schedule.id] = walk_in_schedule
                    walk_in_candidate = _to_candidate(walk_in_schedule)
                    today_candidates = [walk_in_candidate]
                    candidates = [walk_in_candidate]
                    # 방금 생성한 워크인 스케줄을 사용한다. 클라가 이전 (clocked_out)
                    # shift 의 schedule_id 를 실어 보냈더라도 그건 무시 — 안 그러면
                    # 아래 명시-선택 체크에서 새 스케줄과 불일치로 거부된다.
                    schedule_id = None
                elif schedule_id is not None:
                    # 직원이 고른 shift 가 후보 밖이다(끝났거나, 취소됐거나, 4시간 지난
                    # 어제 shift). 여기서 "오늘 스케줄 없음" 으로 답하면 앱은 막다른
                    # 길만 보고 끝난다 — 아래 명시-선택 체크로 흘려보내 구조화된
                    # `shift_not_available` 을 받게 한다(앱은 identify 를 다시 부른다).
                    pass
                elif not today_schedules:
                    raise BadRequestError("No scheduled shift for today at this store")
                else:
                    raise BadRequestError("All today's shifts are already completed")

            # 4) 대상 shift 확정.
            selected = None
            if schedule_id is not None:
                # (Issue 8) client 가 명시적으로 선택한 shift. 후보 밖(clocked_out /
                # 취소 / 4시간 지난 어제 shift)이면 fallback 하지 않고 거부한다 —
                # 앱이 방금 받은 목록에서 고른 값이므로 불일치는 목록이 낡았다는 뜻이고,
                # 앱은 이 코드를 보고 identify 를 다시 불러 picker 를 갱신한다.
                selected = next(
                    (c for c in candidates if c.schedule_id == schedule_id), None
                )
                if selected is None:
                    from app.core.error_codes.attendance import SHIFT_NOT_AVAILABLE

                    raise SHIFT_NOT_AVAILABLE(schedule_id=str(schedule_id))
            else:
                # 서버 fallback — **오늘 후보 중 시간순 첫 미출근** 하나뿐이다(D14).
                # 예전의 "진행중 window → 가장 가까운 미래 → 가장 최근 종료" 우선순위는
                # 2순위가 정확히 원인 A 였다: 오전 shift 를 놓치고 13:05 에 찍으면
                # 17:00 shift 가 잡혀 지각이 조기출근으로 기록됐다.
                selected = pick_fallback_shift(today_candidates)
                if selected is None:
                    # 오늘 후보가 "미출근" 없이 활성 shift 뿐인 경우. 정상 흐름에서는
                    # 위 이중 clock-in 가드가 이미 막았으므로(자동 선택으로는 겹침이
                    # 열리지 않는다) 여기 도달하는 건 상태가 어긋난 기록뿐이다.
                    # 문구는 가드와 같은 것을 쓴다 — 직원이 할 일이 똑같기 때문이다.
                    # (후보가 아예 없는 경우는 위 3) 에서 이미 걸러졌다. 같은 문구를
                    #  두 곳에서 만들지 않는다.)
                    raise BadRequestError(
                        "Previous shift not clocked out. Clock out first."
                    )

            schedule = schedule_by_id[selected.schedule_id]

            # early/late 판정 — 임계값 2종 모두 매장 설정에서 온다.
            # (예전엔 early 만 설정을 읽고 late 는 모듈 상수 0분이라 한 함수 안에서
            #  판정 소스가 갈렸다. 판정 자체는 공용 순수 함수가 한다.)
            from app.services.attendance_service import (
                ANOMALY_EARLY_CLOCK_IN_OVERRIDE,
            )
            from app.services.attendance_threshold_service import (
                resolve_early_clock_in_threshold,
                resolve_late_buffer,
            )
            from app.utils.attendance_judgement import ClockInKind, judge_clock_in

            status_val = "working"
            anomalies: list[str] | None = None
            # 조기 출근 강행 여부 + "이미 승인된 건인가"(매니저 대행) — 확인 게이트용.
            early_clock_in_override = False
            early_override_preapproved = False
            scheduled_start = _start_dt(schedule)
            if scheduled_start is not None:
                early_threshold = await resolve_early_clock_in_threshold(
                    db, organization_id=device.organization_id, store_id=store_id
                )
                late_buffer = await resolve_late_buffer(
                    db, organization_id=device.organization_id, store_id=store_id
                )
                # late/early 판정은 분 단위로만 한다(초는 버림). clock_in 은 초까지 저장하되
                # "정시 출근(같은 분)"이 초 차이로 late 로 찍히지 않게 한다. 워크인은 start=
                # clock-in(분 내림)이라 이 규칙으로 자연히 early/late 가 아니게 된다(특수예외 불필요).
                verdict = judge_clock_in(
                    now,
                    scheduled_start,
                    late_buffer=late_buffer,
                    early_threshold=early_threshold,
                )
                # 조기 clock-in override — 예정보다 이르면 차단이 아니라 "사유 요구".
                # 매니저/SV 가 현장에 없어도 직원이 직접 찍을 수 있어야 하기 때문
                # (일찍 와달라고 부른 경우). 사유가 오면 통과시키고 anomaly 로 표시한다.
                # 상한(몇 시간 전까지) 은 두지 않는다 — 얼마나 일찍 부를지 예측 불가.
                #
                # 매니저 대행(skip_early_guards) 도 **라벨은 똑같이 붙인다** — 예정 밖
                # 근무는 특이사항으로 보여야 하기 때문. 다만 사유는 요구하지 않고,
                # 확인이 이미 끝난 것으로 처리한다(매니저가 그 자리에서 승인한 행위라
                # 다시 확인시키면 이중 확인). 자동 확인은 Phase 2 의 확인 컬럼이 담당.
                if verdict.kind is ClockInKind.EARLY:
                    early_clock_in_override = True
                    early_override_preapproved = skip_early_guards
                    if not skip_early_guards and not (reason and reason.strip()):
                        from fastapi import HTTPException, status

                        minutes_early = verdict.minutes_early
                        # params 는 detail **최상위에 평탄하게** 싣는다(구버전 계약).
                        # `scheduled_start_display` 는 추가 필드일 뿐이라 구버전은 무시한다.
                        start_label = shift_moment_label(scheduled_start, tz)
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail={
                                "code": "early_clock_in_reason_required",
                                "minutes_early": minutes_early,
                                "schedule_id": str(schedule.id),
                                "scheduled_start": scheduled_start.isoformat(),
                                "scheduled_start_display": start_label,
                                "message": (
                                    f"This shift starts {start_label} — in "
                                    f"{minutes_early} minutes. To clock in now, "
                                    "enter a reason — your manager will see it."
                                ),
                            },
                        )
                    anomalies = [ANOMALY_EARLY_CLOCK_IN_OVERRIDE]
                elif verdict.kind is ClockInKind.LATE:
                    # elif — early 와 late 는 배타. 예전엔 두 분기가 각각 anomalies 를
                    # 대입해서 late 가 early 라벨을 지웠다.
                    status_val = "late"
                    anomalies = ["late"]

            # ── 조기 출근 요청자(D9) ────────────────────────────────────────
            # early override 가 성립할 때만 의미를 둔다. 정시/지각 출근에 딸려온 값은
            # 조용히 버린다 — 구버전/오용 내성(에러로 만들면 무해한 요청이 막힌다).
            #
            # 명단 밖 id 는 **조용한 null 저장이 아니라 400** 이다. 앱이 방금 서버에서
            # 받은 명단에서 고른 값이므로 불일치는 클라 버그이거나 변조이고, 조용히
            # 버리면 이 컬럼을 만든 이유(동명이인·개명 추적)가 그대로 사라진다.
            # 앱은 이 코드를 받으면 id 를 떼고 같은 사유로 1회 자동 재시도하므로
            # 키오스크 앞에서 출근이 막히지는 않는다.
            requested_by: UUID | None = None
            if early_clock_in_override and early_clock_in_requested_by is not None:
                from app.core.error_codes.attendance import INVALID_REASON_USER

                valid = await self._is_valid_reason_user(
                    db,
                    candidate_id=early_clock_in_requested_by,
                    organization_id=device.organization_id,
                    store_id=store_id,
                    asking_user_id=user.id,
                )
                if not valid:
                    raise INVALID_REASON_USER()
                requested_by = early_clock_in_requested_by

            # Eager 모델: 이 schedule 에 묶인 attendance row 는 이미 존재해야 함.
            # upcoming/late/no_show 상태에서 clock-in 시 update.
            target = await attendance_repository.get_by_schedule_id(db, schedule.id)
            if target is not None:
                before_status = target.status
                before_clock_in = target.clock_in
                target.store_id = store_id
                target.clock_in = now
                target.clock_in_timezone = store_tz
                target.status = status_val
                existing_anoms = [a for a in (target.anomalies or []) if a != "no_show"]
                if anomalies:
                    for a in anomalies:
                        if a not in existing_anoms:
                            existing_anoms.append(a)
                target.anomalies = existing_anoms or None
                # 이번 clock-in 이 요청자 값의 소유자다 — early 가 아니면 None 으로
                # 되돌린다. 지운 뒤 다시 찍은 기록에 옛 요청자가 유령으로 붙지 않게.
                target.early_clock_in_requested_by = requested_by
                if early_clock_in_override and early_override_preapproved:
                    # 매니저가 그 자리에서 승인한 조기 출근 — 라벨은 남기되 확인은 끝난 것.
                    target.early_clock_in_confirmed_by = manager_user_id
                    target.early_clock_in_confirmed_at = now
                await db.flush()
                await db.refresh(target)
                attendance = target
            else:
                # 예외적인 경우 (eager 훅 누락 등) — 안전망으로 새 row 생성.
                attendance = await attendance_repository.create(
                    db,
                    {
                        "organization_id": device.organization_id,
                        "store_id": store_id,
                        "user_id": user.id,
                        "schedule_id": schedule.id,
                        # 어제 영업일 shift(D4)로 찍으면 급여 귀속도 그 영업일이다.
                        "work_date": selected.operating_day or today,
                        "clock_in": now,
                        "clock_in_timezone": store_tz,
                        "status": status_val,
                        "anomalies": anomalies,
                        "early_clock_in_confirmed_by": (
                            manager_user_id
                            if early_clock_in_override and early_override_preapproved
                            else None
                        ),
                        "early_clock_in_confirmed_at": (
                            now
                            if early_clock_in_override and early_override_preapproved
                            else None
                        ),
                        "early_clock_in_requested_by": requested_by,
                    },
                )

            # 겹침 라벨 — 두 row **모두**에 붙는다. 어느 화면에서 보든 드러나야 하기 때문.
            # 라벨은 표시용이고, 이중 지급을 실제로 막는 것은 payroll 확정 게이트
            # (overlapping_attendance) 의 시간 구간 판정이다.
            if overlap_open_rows:
                from app.services.attendance_service import refresh_overlap_anomaly

                await refresh_overlap_anomaly(
                    db, user_id=user.id, work_date=attendance.work_date, now=now
                )
                await db.flush()
                # 라우터가 응답에 실어 앱이 안내를 띄운다. **표시 문구는 서버가 만들지
                # 않는다** — 앱은 l10n(en/es), 표시 소유권은 클라에 있다.
                attendance._overlap_info = {  # type: ignore[attr-defined]
                    "is_overlapping": True,
                    "other_attendance_ids": [str(r.id) for r in overlap_open_rows],
                    "other_schedule_ids": [
                        str(r.schedule_id) for r in overlap_open_rows if r.schedule_id
                    ],
                }
        elif action == "break_start":
            if attendance is None:
                raise BadRequestError("Must clock in first")
            if attendance.status not in ("working", "late"):
                raise BadRequestError("Cannot start break in current state")
            if break_type not in VALID_BREAK_TYPES:
                raise BadRequestError(
                    "break_type required (paid_10min or unpaid_meal)"
                )
            open_break = await self._get_open_break(db, attendance.id)
            if open_break is not None:
                raise BadRequestError("A break is already in progress")
            new_break = AttendanceBreak(
                attendance_id=attendance.id,
                started_at=now,
                break_type=normalize_break_type(break_type),
            )
            db.add(new_break)
            before_status = attendance.status
            touched_break = new_break
            break_before = (None, None, None)
            attendance.status = "on_break"
            # 하위호환: 기존 컬럼도 최근 break 기준으로 갱신
            attendance.break_start = now
            attendance.break_end = None
            await db.flush()
            await db.refresh(attendance)
        elif action == "break_end":
            if attendance is None:
                raise BadRequestError("Must clock in first")
            if attendance.status != "on_break":
                raise BadRequestError("Not currently on break")
            open_break = await self._get_open_break(db, attendance.id)
            if open_break is None:
                # 상태는 on_break 인데 open row 가 없음 (데이터 불일치) — 보정
                attendance.status = "working"
                await db.flush()
                raise BadRequestError("No open break record")

            # Stage J: break time 정책 검증 (pure helper)
            from app.utils.break_end_policy import validate_break_end
            elapsed_minutes = minutes_between(open_break.started_at, now)
            policy_error = validate_break_end(open_break.break_type, elapsed_minutes, reason)
            if policy_error is not None:
                raise BadRequestError(policy_error)

            before_status = attendance.status
            touched_break = open_break
            break_before = (open_break.started_at, open_break.ended_at, open_break.break_type)
            open_break.ended_at = now
            open_break.duration_minutes = elapsed_minutes
            attendance.status = "working"
            attendance.break_end = now
            # 누적 분 — 새 테이블에서 합산
            total_minutes = await self._sum_break_minutes(db, attendance.id)
            attendance.total_break_minutes = total_minutes
            await db.flush()
            await db.refresh(attendance)
        elif action == "clock_out":
            if attendance is None:
                raise BadRequestError("Must clock in first")
            if attendance.status not in ("working", "late", "on_break"):
                raise BadRequestError("Already clocked out")

            # Early clock-out 검증 — schedule end 의 threshold 이전이면 reason 필수.
            from app.services.attendance_threshold_service import (
                resolve_early_leave_threshold,
            )
            from app.utils.attendance_judgement import judge_clock_out

            is_early = False
            sched_end_dt = None
            if attendance.schedule_id is not None:
                from app.models.schedule import Schedule as _Schedule
                _sch = await db.scalar(
                    select(_Schedule).where(_Schedule.id == attendance.schedule_id)
                )
                if _sch is not None and (_sch.end_at is not None or _sch.end_time is not None):
                    _, sched_end_dt = resolve_schedule_instants(
                        start_at=_sch.start_at, end_at=_sch.end_at, work_date=_sch.work_date,
                        start_time=_sch.start_time, end_time=_sch.end_time, tz_name=store_tz,
                    )
                    _early_thresh = await resolve_early_leave_threshold(
                        db,
                        organization_id=device.organization_id,
                        store_id=store_id,
                    )
                    # 판정은 분 단위 (초 버림) — 20:50 정각 퇴근은 조기가 아니다.
                    is_early = judge_clock_out(
                        now, sched_end_dt, early_leave_threshold=_early_thresh
                    ).is_early
            if not skip_early_guards and is_early and not (reason and reason.strip()):
                raise BadRequestError(
                    "Early clock-out requires a reason. Please provide one."
                )

            before_status = attendance.status
            before_clock_out = attendance.clock_out
            # 진행중 break 가 있으면 먼저 종료 처리
            if attendance.status == "on_break":
                open_break = await self._get_open_break(db, attendance.id)
                if open_break is not None:
                    touched_break = open_break
                    break_before = (
                        open_break.started_at, open_break.ended_at, open_break.break_type,
                    )
                    open_break.ended_at = now
                    open_break.duration_minutes = minutes_between(
                        open_break.started_at, now
                    )
                    attendance.break_end = now
            attendance.clock_out = now
            attendance.clock_out_timezone = store_tz
            attendance.status = "clocked_out"
            if attendance.clock_in is not None:
                attendance.total_work_minutes = minutes_between(attendance.clock_in, now)
            attendance.total_break_minutes = await self._sum_break_minutes(db, attendance.id)

            if is_early:
                anoms = list(attendance.anomalies or [])
                if "early_clock_out" not in anoms:
                    anoms.append("early_clock_out")
                attendance.anomalies = anoms or None
                # early-clock-out 사유는 attendance_corrections 에 기록 (note 더럽히지 않음).
                # 매니저가 console 에서 note 따로 메모하는 영역과 분리.

            await db.flush()
            # 겹침은 파생 라벨이다 — 퇴근으로 구간이 줄면 겹침이 사라질 수 있으므로
            # 만들 수 있는 지점뿐 아니라 **없앨 수 있는 지점**에서도 다시 계산한다.
            from app.services.attendance_service import refresh_overlap_anomaly

            await refresh_overlap_anomaly(
                db, user_id=user.id, work_date=attendance.work_date, now=now
            )
            await db.flush()
            await db.refresh(attendance)
        else:
            raise BadRequestError(f"Invalid action: {action}")

        # ── 모든 attendance 액션을 timeline 에 기록 ──
        # action(카드 태그) 은 매니저 대행이어도 실제 행위(clock_in 등) 를 그대로 쓴다.
        # 예전엔 대행이면 "modify" 로 뭉개서 무엇이 바뀐지 알 수 없었고 before 도 비었다.
        # 대행 여부는 actor(corrected_by) 로 이미 드러난다.
        from app.services import attendance_timeline as tl

        actor_id = manager_user_id if skip_pin_check else user.id
        group = tl.new_group()
        tl.record_status(
            db,
            attendance_id=attendance.id,
            group_id=group,
            action=action,
            before=before_status,
            after=attendance.status,
            reason=reason,
            by_user_id=actor_id,
        )
        if action == "clock_in":
            tl.record(
                db,
                attendance_id=attendance.id,
                group_id=group,
                action=action,
                field_name=tl.FIELD_CLOCK_IN,
                before=tl.dt_value(before_clock_in),
                after=tl.dt_value(attendance.clock_in),
                reason=reason,
                by_user_id=actor_id,
            )
        elif action == "clock_out":
            tl.record(
                db,
                attendance_id=attendance.id,
                group_id=group,
                action=action,
                field_name=tl.FIELD_CLOCK_OUT,
                before=tl.dt_value(before_clock_out),
                after=tl.dt_value(attendance.clock_out),
                reason=reason,
                by_user_id=actor_id,
            )
        if touched_break is not None:
            tl.record_break_snapshot(
                db,
                attendance_id=attendance.id,
                group_id=group,
                action=action,
                break_id=touched_break.id,
                before=break_before,
                after=(
                    touched_break.started_at,
                    touched_break.ended_at,
                    touched_break.break_type,
                ),
                reason=reason,
                by_user_id=actor_id,
            )

        await self.touch_last_seen(db, device)

        # 조기 출근 강행 알림 — 직원이 스스로 찍은 건만. 매니저 대행은 그 매니저가
        # 이미 알고 있으므로 알리지 않는다. 실패해도 출근 자체는 성립해야 한다.
        notify_early = (
            action == "clock_in"
            and early_clock_in_override
            and not early_override_preapproved
        )

        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        if notify_early:
            await self._notify_early_clock_in(
                db,
                attendance=attendance,
                device=device,
                user=user,
                store_tz=store_tz,
                scheduled_start=scheduled_start,
                reason=reason or "",
            )
        if getattr(attendance, "_overlap_info", None) is not None:
            await self._notify_overlapping_clock_in(
                db, attendance=attendance, device=device, user=user
            )
        return attendance

    async def _notify_overlapping_clock_in(
        self,
        db: AsyncSession,
        *,
        attendance: Attendance,
        device: AttendanceDevice,
        user: User,
    ) -> None:
        """겹침 clock-in 매니저 알림 (best-effort).

        직원이 스스로 정리할 수 없는 상태이므로(한쪽을 취소·정정하는 건 매니저 권한)
        매니저가 반드시 알아야 한다. 알림 실패가 출근을 되돌리면 안 되므로 전부 삼킨다.
        단 **삼키되 남긴다** — 개발 중 이 except 가 컬럼명 오타(`Alert.alert_type`)를
        통째로 가려서, 알림이 한 번도 안 나가는데 테스트도 화면도 아무 말이 없었다.
        """
        try:
            from app.services.alert_service import alert_service
            from app.utils.names import display_name

            await alert_service.create_for_overlapping_clock_in(
                db,
                attendance_id=attendance.id,
                organization_id=device.organization_id,
                store_id=attendance.store_id,
                staff_user_id=user.id,
                staff_name=display_name(user),
                work_date=attendance.work_date,
            )
            await db.commit()
        except Exception:
            # 알림 실패는 출근 성립에 영향 없음 — 기록만 남기고 넘어간다.
            logging.getLogger(__name__).exception(
                "[attendance] overlapping clock-in alert failed for %s",
                attendance.id,
            )

    async def _notify_early_clock_in(
        self,
        db: AsyncSession,
        *,
        attendance: Attendance,
        device: AttendanceDevice,
        user: User,
        store_tz: str,
        scheduled_start: datetime | None,
        reason: str,
    ) -> None:
        """조기 출근 강행 in-app 알림 + email (best-effort).

        알림 실패가 출근 기록을 되돌리면 안 된다 — 전체를 삼킨다.
        """
        try:
            from zoneinfo import ZoneInfo as _Zone

            from app.services.alert_service import alert_service
            from app.utils.names import display_name

            if scheduled_start is None or attendance.clock_in is None:
                return
            # 몇 분 일찍인지 — 판정과 같은 분 절삭 규칙(R2)을 쓴다. 여기서 따로
            # 빼기 계산을 하면 알림 문구와 400 응답의 minutes_early 가 갈린다.
            minutes_early = minutes_between(attendance.clock_in, scheduled_start)
            staff_name = display_name(user)
            tz = _Zone(store_tz)
            # 세 채널(400 / in-app / email)이 같은 라벨 포맷을 쓴다 — 날짜 포함.
            scheduled_start_label = shift_moment_label(scheduled_start, tz)
            clock_in_label = shift_moment_label(attendance.clock_in, tz)

            await alert_service.create_for_early_clock_in(
                db,
                attendance_id=attendance.id,
                organization_id=device.organization_id,
                store_id=attendance.store_id,
                staff_user_id=user.id,
                staff_name=staff_name,
                minutes_early=minutes_early,
                scheduled_start_label=scheduled_start_label,
            )
            await db.commit()

            # email — 사용자 선호 가드는 should_send_email 이 담당 (checklist 와 동일).
            import asyncio

            from app.models.organization import Store as _Store
            from app.utils.email import send_email
            from app.utils.email_templates import build_early_clock_in_email

            store_name = await db.scalar(
                select(_Store.name).where(_Store.id == attendance.store_id)
            )
            # 수신자 산출은 in-app 과 **같은 쿼리**를 쓴다. 따로 짜면 두 채널이
            # 조용히 갈린다 (실제로 갈려 있었다 — 둘 다 org 전체로 새어나갔다).
            recipients = await db.execute(
                alert_service.attendance_recipient_query(
                    organization_id=device.organization_id,
                    store_id=attendance.store_id,
                    exclude_user_id=user.id,
                ).where(User.email.is_not(None))
            )
            subject, html = build_early_clock_in_email(
                staff_name=staff_name,
                store_name=store_name or "your store",
                minutes_early=minutes_early,
                scheduled_start_label=scheduled_start_label or "",
                clock_in_label=clock_in_label or "",
                reason=reason or "(no reason provided)",
            )
            for uid, email in recipients.all():
                if not email:
                    continue
                if not await alert_service.should_send_email(
                    db, uid, "early_clock_in_override"
                ):
                    continue
                asyncio.create_task(send_email(to=email, subject=subject, html=html))
        except Exception:
            # 알림 실패는 출근 성립에 영향 없음
            pass

    async def _sum_break_minutes(
        self, db: AsyncSession, attendance_id: UUID
    ) -> int:
        """attendance 의 모든 종료된 break 분 합계."""
        result = await db.execute(
            select(AttendanceBreak.duration_minutes).where(
                AttendanceBreak.attendance_id == attendance_id,
                AttendanceBreak.duration_minutes.is_not(None),
            )
        )
        return sum((v or 0) for v, in result.all())

    async def get_break_summary(
        self, db: AsyncSession, attendance_id: UUID
    ) -> dict:
        """attendance 의 break 합계 (paid/unpaid 분리) + current open break."""
        result = await db.execute(
            select(AttendanceBreak).where(AttendanceBreak.attendance_id == attendance_id)
        )
        breaks = list(result.scalars().all())
        paid = sum(b.duration_minutes or 0 for b in breaks if b.break_type in PAID_BREAK_TYPES)
        unpaid = sum(
            b.duration_minutes or 0 for b in breaks if b.break_type in UNPAID_BREAK_TYPES
        )
        current = next((b for b in breaks if b.ended_at is None), None)
        return {
            "paid_minutes": paid,
            "unpaid_minutes": unpaid,
            "current": current,
            "all": breaks,
        }


attendance_device_service = AttendanceDeviceService()
