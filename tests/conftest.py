"""테스트 인프라 — 임시 PostgreSQL DB, 세션, httpx 클라이언트 픽스처.

Test infrastructure — Temporary PostgreSQL DB, session, and httpx client fixtures.
DB creation/drop uses subprocess (psql) to avoid event loop conflicts.
Schema is applied once per session, data is truncated after each test.
"""

import subprocess
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.database import Base, get_db
from app.main import app
from app.models import *  # noqa: F401,F403 — register all models with metadata
from app.utils.jwt import create_access_token
from app.utils.password import hash_password

# ---------------------------------------------------------------------------
# 테스트 DB 설정
# ---------------------------------------------------------------------------
TEST_DB_NAME = "test_taskmanager"
TEST_DATABASE_URL = f"postgresql+asyncpg://jm@localhost:5432/{TEST_DB_NAME}"

_schema_created = False


# ---------------------------------------------------------------------------
# Session-scoped: DB 생성/삭제 (동기 subprocess)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def create_test_database():
    """세션 시작 시 테스트 DB를 생성하고, 종료 시 삭제합니다."""
    # 기존 연결 끊기 + DB 삭제 + 재생성
    subprocess.run(
        ["psql", "-h", "localhost", "-d", "postgres", "-c",
         f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
         f"WHERE datname = '{TEST_DB_NAME}' AND pid <> pg_backend_pid();"],
        capture_output=True,
    )
    subprocess.run(
        ["psql", "-h", "localhost", "-d", "postgres", "-c",
         f"DROP DATABASE IF EXISTS {TEST_DB_NAME};"],
        capture_output=True,
    )
    subprocess.run(
        ["psql", "-h", "localhost", "-d", "postgres", "-c",
         f"CREATE DATABASE {TEST_DB_NAME};"],
        capture_output=True, check=True,
    )
    yield
    # 세션 종료 후 DB 삭제
    subprocess.run(
        ["psql", "-h", "localhost", "-d", "postgres", "-c",
         f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
         f"WHERE datname = '{TEST_DB_NAME}' AND pid <> pg_backend_pid();"],
        capture_output=True,
    )
    subprocess.run(
        ["psql", "-h", "localhost", "-d", "postgres", "-c",
         f"DROP DATABASE IF EXISTS {TEST_DB_NAME};"],
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Function-scoped: 엔진, 세션, 클라이언트
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def engine(create_test_database) -> AsyncGenerator[AsyncEngine, None]:
    """테스트용 async 엔진. 첫 호출 시 스키마를 생성합니다."""
    global _schema_created
    eng = create_async_engine(TEST_DATABASE_URL, echo=False, pool_pre_ping=True)

    if not _schema_created:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        _schema_created = True

    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """각 테스트에 격리된 DB 세션을 제공합니다."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        yield session
        # 커밋되지 않은 변경 처리
        try:
            await session.commit()
        except Exception:
            await session.rollback()

    # 테스트 후 모든 데이터 정리
    async with factory() as cleanup:
        tables = [t.name for t in reversed(Base.metadata.sorted_tables)]
        if tables:
            await cleanup.execute(text(f"TRUNCATE {', '.join(tables)} CASCADE"))
            await cleanup.commit()


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """FastAPI 테스트 클라이언트 — DB 세션을 오버라이드합니다."""
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 헬퍼 픽스처: 테스트용 데이터 생성
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def org(db: AsyncSession):
    """테스트 조직을 생성합니다."""
    from app.models.organization import Organization
    o = Organization(name="Test Corp", code="TEST01")
    db.add(o)
    await db.flush()
    await db.refresh(o)
    return o


@pytest_asyncio.fixture
async def roles(db: AsyncSession, org):
    """기본 4개 역할을 생성합니다."""
    from app.models.user import Role
    result = {}
    for name, level in [("admin", 1), ("manager", 2), ("supervisor", 3), ("staff", 4)]:
        role = Role(organization_id=org.id, name=name, level=level)
        db.add(role)
        await db.flush()
        await db.refresh(role)
        result[name] = role
    return result


@pytest_asyncio.fixture
async def admin_user(db: AsyncSession, org, roles):
    """관리자 사용자를 생성합니다."""
    from app.models.user import User
    user = User(
        organization_id=org.id,
        role_id=roles["admin"].id,
        username="admin",
        full_name="Test Admin",
        password_hash=hash_password("admin123!"),
        email="admin@test.com",
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


@pytest_asyncio.fixture
async def manager_user(db: AsyncSession, org, roles):
    """매니저 사용자를 생성합니다."""
    from app.models.user import User
    user = User(
        organization_id=org.id,
        role_id=roles["manager"].id,
        username="manager",
        full_name="Test Manager",
        password_hash=hash_password("manager123!"),
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


@pytest_asyncio.fixture
async def staff_user(db: AsyncSession, org, roles):
    """스태프 사용자를 생성합니다."""
    from app.models.user import User
    user = User(
        organization_id=org.id,
        role_id=roles["staff"].id,
        username="staff",
        full_name="Test Staff",
        password_hash=hash_password("staff123!"),
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


@pytest_asyncio.fixture
async def store(db: AsyncSession, org):
    """테스트 매장을 생성합니다."""
    from app.models.organization import Store
    s = Store(organization_id=org.id, name="Test Store", address="123 Test St")
    db.add(s)
    await db.flush()
    await db.refresh(s)
    return s


def make_token(user, role_name: str, role_level: int) -> str:
    """테스트용 JWT 액세스 토큰을 생성합니다."""
    return create_access_token({
        "sub": str(user.id),
        "org": str(user.organization_id),
        "role": role_name,
        "level": role_level,
    })


@pytest.fixture
def admin_token(admin_user, roles) -> str:
    return make_token(admin_user, "admin", 1)


@pytest.fixture
def manager_token(manager_user, roles) -> str:
    return make_token(manager_user, "manager", 2)


@pytest.fixture
def staff_token(staff_user, roles) -> str:
    return make_token(staff_user, "staff", 4)


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
