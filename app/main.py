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
