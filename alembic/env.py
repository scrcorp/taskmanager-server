import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from alembic import context

from app.config import settings
from app.database import Base
from app.models import *  # noqa: F401, F403 — import all models for autogenerate

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


# ── autogenerate 안전장치 ────────────────────────────────────────────────
# 모델에 없지만 **의도적으로 DB 에 남겨둔** 객체들. 이걸 걸러주지 않으면
# `alembic revision --autogenerate` 가 "모델에 없다 = 지워야 한다" 로 판단해
# drop_table / drop_column 을 뱉고, 배포는 `alembic upgrade head` 가 자동으로
# 도니 그 결과를 검토 없이 커밋하는 순간 운영 데이터가 사라진다.
#
# 여기 등록된 것만 예외다. 새로 추가할 땐 반드시 "왜 남겨두는지 + 언제 지울지"를
# 함께 적을 것. 그 외의 diff 는 진짜 드리프트이므로 모델/마이그레이션으로 해소한다.
LEGACY_TABLES: dict[str, str] = {
    # 구 알림 시스템. 현재는 alerts 가 대체하지만 과거 이력 1479행이 남아 있다.
    # 삭제 조건: 이력 보존 판단이 끝나면 명시적 drop 마이그레이션으로 제거.
    "notifications": "구 알림 이력 보존 (alerts 로 대체됨)",
    # 스케줄 신청 기능 폐기(2026-08-09)로 모델/서비스/API 를 제거했으나 테이블은 남긴다.
    # 신청 행 자체는 schedules(status='requested') 에 있었고 이 테이블은 그보다 앞선
    # 세대의 잔존물이다(dev 27행). 드롭하면 되돌릴 수 없어 보존만 한다.
    # 삭제 조건: 이력 보존 판단이 끝나면 명시적 drop 마이그레이션으로 제거.
    "schedule_requests": "폐기된 신청 기능의 구세대 테이블 (이력 보존)",
    "schedule_request_templates": "폐기된 신청 템플릿 테이블 (이력 보존)",
    "schedule_request_template_items": "폐기된 신청 템플릿 항목 테이블 (이력 보존)",
}

LEGACY_COLUMNS: dict[tuple[str, str], str] = {
    # 하위호환 컬럼 — 42abeece1bb2 가 구버전 API 를 위해 되살렸고,
    # 트리거 tr_sync_user_pref_columns 가 alert_preferences 와 양방향 동기화한다.
    # **지우면 트리거가 깨져 users UPDATE 가 전부 실패한다** (실측 확인함).
    # 삭제 조건: 구버전 API 소비자가 사라지면 트리거/함수와 함께 제거.
    ("users", "notification_preferences"): "구버전 API 하위호환 (트리거 동기화 중)",
}


def include_object(object_, name, type_, reflected, compare_to):
    """autogenerate 대상에서 의도적 legacy 객체를 제외한다.

    reflected=True 이고 compare_to=None 이면 "DB 엔 있는데 모델엔 없다" = 삭제 후보.
    그 경우에만 걸러내면 되고, 모델 쪽 변경은 정상적으로 통과시킨다.
    """
    if type_ == "table" and name in LEGACY_TABLES:
        return False
    if type_ == "column":
        table_name = getattr(object_.table, "name", None)
        if (table_name, name) in LEGACY_COLUMNS:
            return False
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # Supabase: transaction pooler(6543) → session pooler(5432) 전환
    # RDS(5432)는 치환 발생 안 하므로 무해
    migration_url = settings.DATABASE_URL.replace(":6543/", ":5432/")
    connectable = create_async_engine(
        migration_url,
        poolclass=pool.NullPool,
        connect_args={
            "statement_cache_size": 0,
        },
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
