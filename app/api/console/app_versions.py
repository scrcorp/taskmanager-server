"""Admin App Versions 라우터 — 모바일 앱 릴리스 카탈로그 관리.

CI 가 새 APK 빌드 후 호출 (POST). 관리자가 수동 조회 (GET list).

⚠️ 임시 결정 (2026-04-29): POST 는 인증 없이 공개. CI 가 service 계정 만들기 전까지
sideload 분량이 적어 보안 위험 낮다고 판단. 추후 require_permission 복구 예정.
"""

from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.database import get_db
from app.models.app_version import AppVersion
from app.models.user import User
from app.schemas.app_version import AppVersionCreateRequest, AppVersionLatestResponse, AppVersionRow
from app.services.app_version_service import app_version_service

router: APIRouter = APIRouter()


@router.post("", response_model=AppVersionRow, status_code=status.HTTP_201_CREATED)
async def create_app_version(
    data: AppVersionCreateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AppVersionRow:
    """새 앱 릴리스 등록 — 현재 인증 없이 호출 가능 (TODO: service 계정 도입 후 다시 잠금).

    is_latest=True 면 같은 채널의 기존 latest를 자동으로 false 로 내림.
    s3_key 는 CI 가 업로드한 객체의 키 (예: app-releases/attendance/v1.0.5/tma.apk).
    """
    row = await app_version_service.create(
        db,
        channel=data.channel,
        version=data.version,
        s3_key=data.s3_key,
        is_latest=data.is_latest,
        is_min_required=data.is_min_required,
        release_notes=data.release_notes,
    )
    await db.commit()
    return AppVersionRow(
        id=str(row.id),
        channel=row.channel,
        version=row.version,
        s3_key=row.s3_key,
        is_latest=row.is_latest,
        is_min_required=row.is_min_required,
        release_notes=row.release_notes,
        released_at=row.released_at,
    )


@router.get("/attendance/latest", response_model=AppVersionLatestResponse)
async def get_attendance_latest(
    _user: Annotated[User, Depends(require_permission("app_versions:read"))],
) -> AppVersionLatestResponse:
    """현재 서버 환경 bucket 의 attendance APK 들 중 버전 가장 높은 것 반환.

    S3 list (또는 local bucket dir) → 파일명/path 에서 버전 파싱 → 최신 선택.
    DB `is_latest` 플래그 의존 X — 새 APK 업로드만 하면 자동으로 최신으로 잡힘.
    Owner/GM 이상만 호출 가능 (app_versions:read permission).
    """
    latest = app_version_service.get_latest_attendance_from_storage()
    if latest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No attendance release found",
        )
    return AppVersionLatestResponse(
        version=latest["version"],
        channel=app_version_service.attendance_channel(),
        download_url=latest["url"],
        released_at=latest["uploaded_at"],
    )


@router.get("", response_model=List[AppVersionRow])
async def list_app_versions(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_permission("app_versions:read"))],
    channel: str | None = None,
) -> List[AppVersionRow]:
    """채널별 릴리스 이력 조회. channel 미지정시 전체."""
    q = select(AppVersion).order_by(AppVersion.released_at.desc())
    if channel:
        q = q.where(AppVersion.channel == channel)
    rows = (await db.execute(q)).scalars().all()
    return [
        AppVersionRow(
            id=str(r.id),
            channel=r.channel,
            version=r.version,
            s3_key=r.s3_key,
            is_latest=r.is_latest,
            is_min_required=r.is_min_required,
            release_notes=r.release_notes,
            released_at=r.released_at,
        )
        for r in rows
    ]
