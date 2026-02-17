"""데이터베이스 엔진 및 세션 설정 모듈.

Database engine and session configuration module.
Sets up the async SQLAlchemy engine, session factory, and ORM base class
for the PostgreSQL database connection via asyncpg.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# 비동기 데이터베이스 엔진 — Async database engine (asyncpg driver)
# pool_pre_ping=True: 커넥션 풀에서 꺼낸 연결의 유효성을 사전 확인 (Validates connections before use)
engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    # Supavisor(트랜잭션 모드 풀러)에서 prepared statement 비활성화
    # Disable prepared statement caches for Supavisor transaction-mode pooling
    connect_args={"statement_cache_size": 0},
)

# 비동기 세션 팩토리 — Async session factory
# expire_on_commit=False: 커밋 후에도 객체 속성 접근 가능 (Allows attribute access after commit without refresh)
async_session: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """SQLAlchemy 선언적 베이스 클래스.

    Declarative base class for all ORM models.
    All models inherit from this class to register with the metadata.
    """

    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """비동기 데이터베이스 세션을 생성하고 요청 종료 시 닫습니다.

    FastAPI dependency that yields an async database session.
    The session is automatically closed after the request completes,
    ensuring no connection leaks.

    Yields:
        AsyncSession: SQLAlchemy 비동기 세션 인스턴스 (Async session instance)
    """
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
