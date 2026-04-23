"""FastAPI 애플리케이션 엔트리포인트 — 미들웨어 및 라우터 등록.

FastAPI application entry point — Middleware and router registration.
Configures CORS, health check, and includes routers for each phase.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.middleware.axiom_logging import AxiomLoggingMiddleware

app: FastAPI = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Axiom API 로깅 미들웨어 — Axiom API request/response logging
# CORS보다 먼저 등록하여 모든 요청을 캡처 (Registered before CORS to capture all requests)
app.add_middleware(AxiomLoggingMiddleware)

# CORS 미들웨어 — Cross-Origin Resource Sharing middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """서버 상태 확인 엔드포인트.

    Health check endpoint for load balancers and monitoring.
    """
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 라우터 등록 — Phase 1~3 모든 엔드포인트 통합
# Router registration — All Phase 1~3 endpoints aggregated in sub-packages
# ---------------------------------------------------------------------------
# admin_router: Phase 1(Foundation) + Phase 2(Core Workflow) + Phase 3(Communication)
# app_router: Phase 1(Auth/Profile) + Phase 2(Assignments) + Phase 3(Announcements/Tasks/Notifications)
from app.api.auth import router as common_auth_router  # noqa: E402
from app.api.admin import admin_router  # noqa: E402
from app.api.app import app_router  # noqa: E402
from app.api.admin.setup import router as setup_page_router  # noqa: E402

app.include_router(common_auth_router, prefix="/api/v1/auth", tags=["Auth"])
app.include_router(admin_router, prefix="/api/v1/admin")
app.include_router(app_router, prefix="/api/v1/app")
app.include_router(setup_page_router, tags=["Setup Page"])

# Attendance Device 전용 라우터 — JWT 와 별개 auth scope (device token)
from app.api.attendance_device import router as attendance_device_router  # noqa: E402
app.include_router(attendance_device_router, prefix="/api/v1/attendance", tags=["Attendance Device"])

# 로컬 버킷 정적 파일 서빙 — Local bucket static file serving
from app.services.storage_service import BUCKET_DIR  # noqa: E402

BUCKET_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/bucket", StaticFiles(directory=str(BUCKET_DIR)), name="bucket")


# ---------------------------------------------------------------------------
# Startup: APScheduler — attendance state cron (every 1 min)
# ---------------------------------------------------------------------------
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: E402
from apscheduler.triggers.interval import IntervalTrigger  # noqa: E402

scheduler: AsyncIOScheduler = AsyncIOScheduler()


@app.on_event("startup")
async def start_scheduler() -> None:
    """APScheduler 시작 — attendance 자동 상태 전환 cron."""
    import logging
    from app.services.attendance_cron_service import run_attendance_state_tick

    logger = logging.getLogger("uvicorn.error")
    if not scheduler.running:
        scheduler.add_job(
            run_attendance_state_tick,
            trigger=IntervalTrigger(minutes=1),
            id="attendance_state_tick",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        logger.info("[scheduler] APScheduler started with attendance_state_tick job")


@app.on_event("shutdown")
async def stop_scheduler() -> None:
    """APScheduler 종료."""
    if scheduler.running:
        scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Startup: Settings Registry seed (upsert missing entries)
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def seed_settings_registry() -> None:
    """SETTINGS_SEED 정의를 settings_registry 테이블에 upsert.

    이미 존재하는 키는 건드리지 않는다 (사용자가 수정했을 수 있음).
    새로 추가된 키만 INSERT.
    """
    import logging
    from sqlalchemy import select
    from app.database import async_session
    from app.models.settings import SettingsRegistry
    from app.seeds.settings_seed import SETTINGS_SEED

    logger = logging.getLogger("uvicorn.error")
    try:
        async with async_session() as db:
            # 기존 키 목록
            existing_result = await db.execute(select(SettingsRegistry.key))
            existing_keys = {row[0] for row in existing_result.all()}

            inserted = 0
            for definition in SETTINGS_SEED:
                if definition.key in existing_keys:
                    continue
                entry = SettingsRegistry(
                    key=definition.key,
                    label=definition.label,
                    description=definition.description,
                    value_type=definition.value_type,
                    levels=definition.levels,
                    default_priority=definition.default_priority,
                    default_value=definition.default_value,
                    validation_schema=definition.validation_schema,
                    category=definition.category,
                )
                db.add(entry)
                inserted += 1

            if inserted > 0:
                await db.commit()
                logger.info(f"[settings_seed] Inserted {inserted} new registry entries")
    except Exception as e:
        logger.warning(f"Failed to seed settings registry: {e}")


# ---------------------------------------------------------------------------
# Startup: ensure all organizations have a default daily report template
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def ensure_daily_report_templates() -> None:
    """Check all organizations and create default template for those missing one."""
    import logging
    from sqlalchemy import select, func
    from app.database import async_session
    from app.models.organization import Organization
    from app.models.daily_report import DailyReportTemplate
    from app.services.daily_report_service import daily_report_service

    logger = logging.getLogger("uvicorn.error")
    try:
        async with async_session() as db:
            # Find orgs that have no templates at all
            orgs_with_template = (
                select(DailyReportTemplate.organization_id)
                .where(DailyReportTemplate.organization_id.isnot(None))
                .distinct()
            )
            orgs_without = await db.execute(
                select(Organization.id).where(Organization.id.notin_(orgs_with_template))
            )
            org_ids = [row[0] for row in orgs_without.fetchall()]

            if org_ids:
                for org_id in org_ids:
                    await daily_report_service.create_default_template_for_org(db, org_id)
                    logger.info(f"Created default daily report template for org {org_id}")
                await db.commit()
    except Exception as e:
        logger.warning(f"Failed to ensure daily report templates: {e}")


# ---------------------------------------------------------------------------
# Startup: Attendance access code bootstrap
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def bootstrap_attendance_access_code() -> None:
    """attendance service_key 의 access code 를 보장.

    .env 의 ATTENDANCE_ACCESS_CODE 가 있으면 upsert, 없으면 기존 DB 값 유지,
    그것도 없으면 자동 생성.
    """
    import logging

    from app.core.access_code import ensure_code
    from app.database import async_session

    logger = logging.getLogger("uvicorn.error")
    try:
        async with async_session() as db:
            record = await ensure_code(db, "attendance", env_var_name="ATTENDANCE_ACCESS_CODE")
            await db.commit()
            if record.source == "auto":
                logger.info(f"[access_code] attendance code (auto): {record.code}")
            else:
                logger.info(f"[access_code] attendance code loaded from env")
    except Exception as e:
        logger.warning(f"Failed to bootstrap attendance access code: {e}")


# ---------------------------------------------------------------------------
# Startup: Permission Registry sync (DB에 없는 permission 자동 추가)
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def sync_permission_registry() -> None:
    """PERMISSION_REGISTRY → DB permissions 테이블 동기화.

    이미 존재하는 코드는 description만 업데이트.
    새로 추가된 코드는 INSERT.
    Owner는 require_permission()에서 bypass하므로 role_permissions 불필요.
    """
    import logging
    from sqlalchemy import select
    from app.database import async_session
    from app.models.permission import Permission
    from app.core.permissions import PERMISSION_REGISTRY

    logger = logging.getLogger("uvicorn.error")
    try:
        async with async_session() as db:
            result = await db.execute(select(Permission))
            existing = {p.code: p for p in result.scalars().all()}

            new_permission_ids: list = []
            inserted, updated = 0, 0
            for code, resource, action, description, require_priority_check in PERMISSION_REGISTRY:
                if code in existing:
                    p = existing[code]
                    changed = False
                    if p.description != description:
                        p.description = description
                        changed = True
                    if p.require_priority_check != require_priority_check:
                        p.require_priority_check = require_priority_check
                        changed = True
                    if changed:
                        updated += 1
                else:
                    perm = Permission(
                        code=code,
                        resource=resource,
                        action=action,
                        description=description,
                        require_priority_check=require_priority_check,
                    )
                    db.add(perm)
                    inserted += 1
                    new_permission_ids.append(perm)

            if inserted or updated:
                await db.flush()

            if inserted or updated:
                await db.commit()
                logger.info(f"[permission_sync] Inserted {inserted}, updated {updated} permissions")
    except Exception as e:
        logger.warning(f"Failed to sync permission registry: {e}")


# ---------------------------------------------------------------------------
# Startup: Role → Permission 기본값 backfill
# 신규 permission 이 REGISTRY 에 추가되거나 새 DB 에 역할이 있는데
# role_permissions 가 비어있을 때 DEFAULT_ROLE_PERMISSIONS 기준으로 누락분만 INSERT.
# 이미 존재하는 role_permissions 는 건드리지 않음 (admin 수동 조정 보호).
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def sync_default_role_permissions() -> None:
    """DEFAULT_ROLE_PERMISSIONS 기준으로 role_permissions 누락분 채우기.

    role.name 이 'owner' / 'general_manager' / 'supervisor' / 'staff' 인 경우에만
    매핑. 커스텀 role 은 건드리지 않음. 이미 존재하는 (role, permission) 쌍은 skip.
    """
    import logging

    from sqlalchemy import select
    from app.database import async_session
    from app.models.permission import Permission, RolePermission
    from app.models.user import Role
    from app.core.permissions import DEFAULT_ROLE_PERMISSIONS

    # DEFAULT_ROLE_PERMISSIONS 의 키는 'gm' 등 축약형일 수 있음. role.name 매핑 테이블.
    ROLE_NAME_TO_DEFAULT_KEY = {
        "owner": "owner",
        "general_manager": "gm",
        "supervisor": "sv",
        "staff": "staff",
    }

    logger = logging.getLogger("uvicorn.error")
    try:
        async with async_session() as db:
            perms = (await db.execute(select(Permission))).scalars().all()
            perms_by_code = {p.code: p for p in perms}

            roles = (await db.execute(select(Role))).scalars().all()
            if not roles:
                return

            existing = (await db.execute(select(RolePermission))).scalars().all()
            have = {(rp.role_id, rp.permission_id) for rp in existing}

            added = 0
            for role in roles:
                default_key = ROLE_NAME_TO_DEFAULT_KEY.get(role.name)
                if default_key is None:
                    continue
                default_codes = DEFAULT_ROLE_PERMISSIONS.get(default_key, set())
                for code in default_codes:
                    perm = perms_by_code.get(code)
                    if perm is None:
                        continue
                    if (role.id, perm.id) in have:
                        continue
                    db.add(RolePermission(role_id=role.id, permission_id=perm.id))
                    added += 1

            if added:
                await db.commit()
                logger.info(
                    f"[role_permissions_sync] Added {added} missing default role_permissions"
                )
    except Exception as e:
        logger.warning(f"Failed to sync default role_permissions: {e}")
