"""FastAPI 애플리케이션 엔트리포인트 — 미들웨어 및 라우터 등록.

FastAPI application entry point — Middleware and router registration.
Configures CORS, health check, and includes routers for each phase.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.middleware.app_version_broadcast import AppVersionBroadcastMiddleware
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

# Attendance 응답에 X-App-Latest-Version 등 piggyback 헤더 추가
app.add_middleware(AppVersionBroadcastMiddleware)

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
# console_router: Phase 1(Foundation) + Phase 2(Core Workflow) + Phase 3(Communication)
# app_router: Phase 1(Auth/Profile) + Phase 2(Assignments) + Phase 3(Notices/Tasks/Alerts)
from app.api.auth import router as common_auth_router  # noqa: E402
from app.api.console import console_router  # noqa: E402
from app.api.app import app_router  # noqa: E402
from app.api.console.setup import router as setup_page_router  # noqa: E402

app.include_router(common_auth_router, prefix="/api/v1/auth", tags=["Auth"])
app.include_router(console_router, prefix="/api/v1/console")
app.include_router(app_router, prefix="/api/v1/app")
app.include_router(setup_page_router, tags=["Setup Page"])

# Control Plane — 플랫폼 운영자 전용 평면 (org 권한 밖). 비밀경로 슬러그로 마운트.
# 비밀경로+운영자 해시가 설정된 경우에만 활성 (settings.control_plane_enabled).
if settings.control_plane_enabled:
    from app.api.control import control_router  # noqa: E402

    app.include_router(
        control_router,
        prefix="/" + settings.CONTROL_PLANE_PATH.strip("/"),
        include_in_schema=False,
    )

# Attendance Device 전용 라우터 — JWT 와 별개 auth scope (device token)
from app.api.attendance import router as attendance_router  # noqa: E402
app.include_router(attendance_router, prefix="/api/v1/attendance", tags=["Attendance Device"])

# Public 라우터 — 인증 없음 (htma-download 등 매장 staff 공유용)
from app.api.public_releases import router as public_releases_router  # noqa: E402
app.include_router(public_releases_router, prefix="/api/v1/public", tags=["Public"])

# 로컬 버킷 정적 파일 서빙 — Local bucket static file serving
from app.services.storage_service import BUCKET_DIR  # noqa: E402

BUCKET_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/bucket", StaticFiles(directory=str(BUCKET_DIR)), name="bucket")


# ---------------------------------------------------------------------------
# Startup: APScheduler — attendance state cron (every 1 min)
# ---------------------------------------------------------------------------
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402
from apscheduler.triggers.interval import IntervalTrigger  # noqa: E402

scheduler: AsyncIOScheduler = AsyncIOScheduler()


@app.on_event("startup")
async def start_scheduler() -> None:
    """APScheduler 시작 — attendance state cron + 스케줄 일일 리포트."""
    import logging
    from app.services.attendance_cron_service import run_attendance_state_tick
    from app.services.schedule_report_service import run_daily_report_tick

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
        # 각 org timezone 기준 15시. scheduler는 UTC지만 service 안에서 org tz로 today를 재계산하므로
        # 한국/미국 orgs 모두 자기 timezone 15시 근방에 발송될 수 있도록 매 hour 0분에 깨운 뒤
        # tz별 15시인 org만 발송하는 방식이 정석이지만, 현재 단일 org 기준이라 hour=15 단일 트리거로 충분.
        # 운영 timezone은 settings.SCHEDULE_REPORT_TIMEZONE 에서 지정.
        report_tz_name = settings.SCHEDULE_REPORT_TIMEZONE or "UTC"
        scheduler.add_job(
            run_daily_report_tick,
            trigger=CronTrigger(hour=15, minute=0, timezone=report_tz_name),
            id="schedule_daily_report",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        logger.info("[scheduler] APScheduler started (attendance_state_tick, schedule_daily_report tz=%s)", report_tz_name)


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
async def ensure_issue_default_template() -> None:
    """System default issue template (org_id=NULL, store_id=NULL) 1건 보장.

    매장이 customize 안 한 경우의 fallback. 6개 기본 카테고리 시드.
    """
    import logging
    from sqlalchemy import select
    from app.database import async_session
    from app.models.report import ReportTemplate
    from app.schemas.report import DEFAULT_ISSUE_CATEGORIES

    logger = logging.getLogger("uvicorn.error")
    try:
        async with async_session() as db:
            existing = await db.execute(
                select(ReportTemplate).where(
                    ReportTemplate.type == "issue",
                    ReportTemplate.organization_id.is_(None),
                    ReportTemplate.store_id.is_(None),
                    ReportTemplate.is_default.is_(True),
                )
            )
            if existing.scalar_one_or_none():
                return
            categories = [
                {
                    "code": code,
                    "label": code.replace("_", " ").title(),
                    "color": None,
                    "sort_order": idx + 1,
                    "is_active": True,
                }
                for idx, code in enumerate(DEFAULT_ISSUE_CATEGORIES)
            ]
            db.add(
                ReportTemplate(
                    type="issue",
                    organization_id=None,
                    store_id=None,
                    name="Default Issue Form",
                    is_default=True,
                    is_active=True,
                    payload={"categories": categories, "custom_fields": []},
                )
            )
            await db.commit()
            logger.info("Created system default issue template")
    except Exception as e:
        logger.warning(f"Failed to ensure default issue template: {e}")


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
        "super_owner": "super_owner",
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


# ---------------------------------------------------------------------------
# Startup: Evaluation Basic template bootstrap
# ---------------------------------------------------------------------------
# 모든 조직에 is_default Basic 평가 템플릿 1개를 보장 (없는 조직만 backfill).
# v1 에서 템플릿이 생성되는 유일한 경로(startup 시드 + 신규 org setup).
@app.on_event("startup")
async def ensure_evaluation_basic_template() -> None:
    """모든 조직에 Basic 평가 템플릿(is_default)을 보장. Idempotent.

    이미 default 가 있는 조직은 skip. evaluation_service.ensure_basic_template
    단일 소스를 호출. 시드 실패가 startup 을 막지 않도록 try/except + warning.
    """
    import logging

    from sqlalchemy import select
    from app.database import async_session
    from app.models.organization import Organization
    from app.services.evaluation_service import evaluation_service

    logger = logging.getLogger("uvicorn.error")
    try:
        async with async_session() as db:
            org_ids = [
                row[0]
                for row in (await db.execute(select(Organization.id))).fetchall()
            ]
            created = 0
            for org_id in org_ids:
                template = await evaluation_service.ensure_basic_template(db, org_id)
                # ensure_basic_template 은 flush 만 — 새로 add 된 경우 created 표시.
                if template is not None and template.created_at is None:
                    created += 1
            await db.commit()
            if org_ids:
                logger.info(
                    f"[evaluation_template] Ensured Basic template for {len(org_ids)} org(s)"
                )
    except Exception as e:
        logger.warning(f"Failed to ensure evaluation Basic templates: {e}")
