"""FastAPI 애플리케이션 엔트리포인트 — 미들웨어 및 라우터 등록.

FastAPI application entry point — Middleware and router registration.
Configures CORS, health check, and includes routers for each phase.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
from app.api.admin import admin_router  # noqa: E402
from app.api.app import app_router  # noqa: E402
from app.api.admin.setup import router as setup_page_router  # noqa: E402

app.include_router(admin_router, prefix="/api/v1/admin")
app.include_router(app_router, prefix="/api/v1/app")
app.include_router(setup_page_router, tags=["Setup Page"])
