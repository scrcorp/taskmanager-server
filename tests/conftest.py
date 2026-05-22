"""테스트 공통 픽스처.

전략:
    - 빈 DB / 부분 시드 DB / 완전 시드 DB 어디서든 동일하게 동작 (idempotent seed).
    - `seed_organization` → `seed_roles` → `test_users` → `test_store_id` 순으로
      필요한 데이터를 ensure. 이미 있으면 재사용, 없으면 생성.
    - 각 테스트 전후에 attendance/schedule/notice 임시 데이터 정리.
    - 시드 자체(organization, role, user, store) 는 session scope — 한 번 만들고 재사용.
    - access_code 는 startup 시 서버가 ensure. 없으면 conftest 가 fallback ensure.
    - httpx.AsyncClient 는 ASGITransport(app) 로 실제 네트워크 없이 호출.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timezone
from typing import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

# Prevent scheduler startup noise in tests
os.environ.setdefault("DEBUG", "false")

from app.database import async_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models.attendance import Attendance  # noqa: E402
from app.models.attendance_break import AttendanceBreak  # noqa: E402  (FK CASCADE 로 자동 삭제, import 만 유지)
from app.models.attendance_device import AttendanceDevice  # noqa: E402
from app.models.communication import Notice  # noqa: E402
from app.models.organization import Organization, Store  # noqa: E402
from app.models.schedule import Schedule  # noqa: E402
from app.models.user import Role, User  # noqa: E402


# ---------------------------------------------------------------------------
# 기본 DB 세션 팩토리 접근 (scope=function)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """테스트에서 직접 DB 조작/검증 시 사용하는 세션."""
    async with async_session() as session:
        yield session


# ---------------------------------------------------------------------------
# Seed: organization (idempotent)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def seed_organization() -> dict:
    """첫 organization. 없으면 만듦. dict 로 반환 (id/name).

    Session scope — 한 번 만들고 모든 테스트에서 재사용.
    """
    async with async_session() as db:
        org = (
            await db.execute(
                select(Organization).order_by(Organization.created_at).limit(1)
            )
        ).scalar_one_or_none()
        if org is None:
            org = Organization(name="Test Organization")
            db.add(org)
            await db.commit()
            await db.refresh(org)
        return {"id": org.id, "name": org.name}


# ---------------------------------------------------------------------------
# Seed: roles (5개, idempotent)
# ---------------------------------------------------------------------------


_ROLE_SPECS: list[tuple[str, int]] = [
    ("super_owner", 5),
    ("owner", 10),
    ("general_manager", 20),
    ("supervisor", 30),
    ("staff", 40),
]


@pytest_asyncio.fixture(scope="session")
async def seed_roles(seed_organization: dict) -> dict[str, UUID]:
    """5개 role 을 ensure. {name: id}."""
    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        existing = {
            r.name: r.id
            for r in (
                await db.execute(
                    select(Role).where(Role.organization_id == org_id)
                )
            ).scalars().all()
        }
        for name, priority in _ROLE_SPECS:
            if name not in existing:
                role = Role(organization_id=org_id, name=name, priority=priority)
                db.add(role)
                await db.commit()
                await db.refresh(role)
                existing[name] = role.id
        return existing


# ---------------------------------------------------------------------------
# Seed: test users (4개, idempotent)
# ---------------------------------------------------------------------------


_USER_SPECS: list[tuple[str, str, str]] = [
    # (username, full_name, role_name)
    ("testadmin", "Test Admin", "super_owner"),
    ("testgm", "Test GM", "general_manager"),
    ("testsv", "Test SV", "supervisor"),
    ("teststaff", "Test Staff", "staff"),
]


@pytest_asyncio.fixture(scope="session")
async def test_users(seed_organization: dict, seed_roles: dict[str, UUID]) -> dict[str, dict]:
    """4개 test user idempotent ensure. {username: {id/clockin_pin/...}}.

    이미 있으면 그대로 사용 (단, is_active=true 보장, clockin_pin 없으면 생성).
    없으면 password='1234' / 새 PIN 로 생성.
    """
    from app.services.attendance_device_service import generate_clockin_pin
    from app.utils.password import hash_password

    org_id: UUID = seed_organization["id"]
    usernames = [s[0] for s in _USER_SPECS]

    async with async_session() as db:
        existing = {
            u.username: u
            for u in (
                await db.execute(
                    select(User).where(
                        User.username.in_(usernames),
                        User.organization_id == org_id,
                    )
                )
            ).scalars().all()
        }

        # password_hash 캐시 — 4명 모두 "1234" 라 한 번만 hash
        password_hash_cache: str | None = None

        for username, full_name, role_name in _USER_SPECS:
            if username not in existing:
                if password_hash_cache is None:
                    password_hash_cache = hash_password("1234")
                user = User(
                    organization_id=org_id,
                    role_id=seed_roles[role_name],
                    username=username,
                    full_name=full_name,
                    password_hash=password_hash_cache,
                    clockin_pin=generate_clockin_pin(),
                    is_active=True,
                )
                db.add(user)
                await db.commit()
                await db.refresh(user)
                existing[username] = user
            else:
                # 활성화/PIN 보장
                u = existing[username]
                dirty = False
                if not u.is_active:
                    u.is_active = True
                    dirty = True
                if not u.clockin_pin:
                    u.clockin_pin = generate_clockin_pin()
                    dirty = True
                if u.deleted_at is not None:
                    u.deleted_at = None
                    dirty = True
                if dirty:
                    await db.commit()
                    await db.refresh(u)

    return {
        u.username: {
            "id": u.id,
            "clockin_pin": u.clockin_pin,
            "organization_id": u.organization_id,
            "full_name": u.full_name,
            "password_hash": u.password_hash,
        }
        for u in existing.values()
    }


@pytest.fixture
def test_user(test_users: dict) -> dict:
    """기본 테스트 유저 = teststaff (staff 권한)."""
    return test_users["teststaff"]


# ---------------------------------------------------------------------------
# 공용 테스트 매장 — day_start_time UTC 00:00 강제
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def test_store_id(seed_organization: dict) -> UUID:
    """`__attendance_test_store__` 매장을 보장하고 id 반환.

    timezone=UTC, day_start_time={'all':'00:00'} — work_date = UTC 캘린더 날짜.
    """
    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        result = await db.execute(
            select(Store).where(
                Store.organization_id == org_id,
                Store.name == "__attendance_test_store__",
            )
        )
        store = result.scalar_one_or_none()
        if store is None:
            store = Store(
                organization_id=org_id,
                name="__attendance_test_store__",
                timezone="UTC",
                day_start_time={"all": "00:00"},
            )
            db.add(store)
            await db.commit()
            await db.refresh(store)
        else:
            # 기존 값이 있어도 강제로 UTC/00:00 으로 normalize
            store.timezone = "UTC"
            store.day_start_time = {"all": "00:00"}
            store.deleted_at = None
            store.is_active = True
            await db.commit()
        return store.id


@pytest_asyncio.fixture(scope="session")
async def second_store_id(seed_organization: dict) -> UUID:
    """조직 내 두 번째 매장 — list_stores / notices 스코프 테스트용."""
    org_id: UUID = seed_organization["id"]
    async with async_session() as db:
        result = await db.execute(
            select(Store).where(
                Store.organization_id == org_id,
                Store.name == "__attendance_test_store_B__",
            )
        )
        store = result.scalar_one_or_none()
        if store is None:
            store = Store(
                organization_id=org_id,
                name="__attendance_test_store_B__",
                timezone="UTC",
                day_start_time={"all": "00:00"},
            )
            db.add(store)
            await db.commit()
            await db.refresh(store)
        return store.id


# ---------------------------------------------------------------------------
# HTTP 클라이언트
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def async_client(_clean_state) -> AsyncIterator[AsyncClient]:
    """ASGITransport 로 FastAPI 앱 직접 호출. `_clean_state` 의존으로 매 테스트
    DB 초기화를 보장."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# Access code — 서버 startup 이 DB 에 upsert. 없으면 fallback ensure.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def attendance_access_code() -> str:
    async with async_session() as db:
        result = await db.execute(
            text("SELECT code FROM access_codes WHERE service_key='attendance'")
        )
        code = result.scalar_one_or_none()
    if not code:
        from app.core.access_code import ensure_code

        async with async_session() as db:
            record = await ensure_code(db, "attendance", env_var_name="ATTENDANCE_ACCESS_CODE")
            await db.commit()
            code = record.code
    return code


# ---------------------------------------------------------------------------
# Device token — register 호출해서 받은 토큰을 Authorization 헤더로 사용
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def device_token(
    async_client: AsyncClient,
    attendance_access_code: str,
    test_store_id: UUID,
    _session_created_device_ids: list,
) -> str:
    """신규 기기 등록 후 토큰 반환. 자동으로 test_store_id 할당."""
    resp = await async_client.post(
        "/api/v1/attendance/register",
        json={"access_code": attendance_access_code, "fingerprint": "pytest"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    token = body["token"]
    device_id = body["device_id"]
    _session_created_device_ids.append(UUID(device_id))

    # 매장 지정
    resp2 = await async_client.put(
        "/api/v1/attendance/store",
        headers={"Authorization": f"Bearer {token}"},
        json={"store_id": str(test_store_id)},
    )
    assert resp2.status_code == 200, resp2.text
    return token


@pytest_asyncio.fixture
async def device_auth_headers(device_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {device_token}"}


@pytest_asyncio.fixture
async def unassigned_device_token(
    async_client: AsyncClient,
    attendance_access_code: str,
    _session_created_device_ids: list,
) -> str:
    """store_id 가 null 인 기기 — 등록만 한 상태."""
    resp = await async_client.post(
        "/api/v1/attendance/register",
        json={"access_code": attendance_access_code, "fingerprint": "pytest-no-store"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    _session_created_device_ids.append(UUID(body["device_id"]))
    return body["token"]


# ---------------------------------------------------------------------------
# 세션 전역 추적용 — 테스트가 만든 device 를 session 끝에 정리
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _session_created_device_ids() -> list:
    return []


# ---------------------------------------------------------------------------
# 스케줄 팩토리 — 오늘(UTC) confirmed 스케줄 생성
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def make_schedule(test_store_id: UUID, _tracked_schedule_ids: list):
    """test_store_id + 오늘 날짜로 confirmed schedule 생성하는 팩토리."""

    async def _factory(
        user_info: dict,
        *,
        store_id: UUID | None = None,
        work_date: date | None = None,
        start_time: time | None = time(9, 0),
        end_time: time | None = time(17, 0),
    ) -> UUID:
        from app.utils.timezone import get_store_day_config, get_work_date

        async with async_session() as db:
            sid = store_id or test_store_id
            tz_name, day_cfg = await get_store_day_config(db, sid)
            wd = work_date or get_work_date(tz_name, day_cfg, datetime.now(timezone.utc))
            sched = Schedule(
                organization_id=user_info["organization_id"],
                user_id=user_info["id"],
                store_id=sid,
                work_date=wd,
                start_time=start_time,
                end_time=end_time,
                status="confirmed",
            )
            db.add(sched)
            await db.commit()
            await db.refresh(sched)
            _tracked_schedule_ids.append(sched.id)
            return sched.id

    return _factory


@pytest.fixture
def _tracked_schedule_ids() -> list:
    return []


@pytest_asyncio.fixture
async def test_schedule(make_schedule, test_user) -> UUID:
    """test_user 의 오늘 confirmed 스케줄.

    start_time 을 약간 미래로 (now+30m) — 'working' 상태로 보이게.
    자정 근처는 23:59 로 고정.
    """
    from datetime import datetime as _dt, time as _time, timedelta as _td, timezone as _tz

    now_utc = _dt.now(_tz.utc)
    target = now_utc + _td(minutes=30)
    if target.date() != now_utc.date():
        start_t = _time(23, 59)
        end_t = _time(23, 59)
    else:
        start_t = target.time().replace(microsecond=0)
        end_dt = now_utc + _td(hours=8)
        if end_dt.date() != now_utc.date():
            end_t = _time(23, 59)
        else:
            end_t = end_dt.time().replace(microsecond=0)
    return await make_schedule(test_user, start_time=start_t, end_time=end_t)


# ---------------------------------------------------------------------------
# Cleanup — 테스트 전후 데이터 정리
# ---------------------------------------------------------------------------


async def _purge_test_data(
    test_users: dict,
    test_store_id: UUID,
    second_store_id: UUID,
) -> None:
    """attendance / schedule / 테스트 notice 정리. 시드(user/store/org) 는 안 건드림."""
    user_ids: list[UUID] = [info["id"] for info in test_users.values()]
    store_ids: list[UUID] = [test_store_id, second_store_id]
    async with async_session() as db:
        # attendance_breaks 는 attendance FK CASCADE 로 같이 삭제됨
        await db.execute(
            delete(Attendance).where(
                Attendance.user_id.in_(user_ids),
                Attendance.store_id.in_(store_ids),
            )
        )
        await db.execute(
            delete(Schedule).where(
                Schedule.user_id.in_(user_ids),
                Schedule.store_id.in_(store_ids),
            )
        )
        await db.execute(
            delete(Notice).where(Notice.title.like("__TEST__%"))
        )
        await db.commit()


@pytest_asyncio.fixture
async def _clean_state(
    test_users,
    test_store_id,
    second_store_id,
    _tracked_schedule_ids,
):
    """테스트 시작 전 깨끗한 상태 보장, 종료 시에도 정리."""
    await _purge_test_data(test_users, test_store_id, second_store_id)
    _tracked_schedule_ids.clear()
    try:
        yield
    finally:
        await _purge_test_data(test_users, test_store_id, second_store_id)


# ---------------------------------------------------------------------------
# PIN 복원 — regenerate / update 테스트가 PIN 바꾼 경우 원복
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def restore_pins(test_users):
    """테스트가 유저 PIN 을 변경하면 원래 값으로 복원."""
    originals = {info["id"]: info["clockin_pin"] for info in test_users.values()}
    yield
    async with async_session() as db:
        for uid, pin in originals.items():
            await db.execute(
                text("UPDATE users SET clockin_pin=:pin WHERE id=:id"),
                {"pin": pin, "id": str(uid)},
            )
        await db.commit()


# ---------------------------------------------------------------------------
# Session teardown — 만들어진 device 정리
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _session_teardown(_session_created_device_ids):
    yield
    if not _session_created_device_ids:
        return
    async with async_session() as db:
        await db.execute(
            delete(AttendanceDevice).where(
                AttendanceDevice.id.in_(_session_created_device_ids)
            )
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Admin JWT — testadmin 으로 로그인해서 access_token 을 세션 내 캐시
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def admin_token(test_users) -> str:
    """testadmin 으로 console login. test_users 의존 — 시드 보장 후 로그인."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/console/auth/login",
            json={"username": "testadmin", "password": "1234"},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def admin_headers(admin_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}
