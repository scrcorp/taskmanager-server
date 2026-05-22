"""Attendance app 버전 정보 라우터.

`/api/v1/attendance` 하위에 mount.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_attendance_device
from app.database import get_db
from app.models.attendance_device import AttendanceDevice
from app.schemas.app_version import AppVersionResponse
from app.services.app_version_service import app_version_service


router: APIRouter = APIRouter()


@router.get("/app-version", response_model=AppVersionResponse)
async def get_app_version(
    device: Annotated[AttendanceDevice, Depends(get_current_attendance_device)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AppVersionResponse:
    """현재 환경 attendance 채널의 최신/최소 버전 + 다운로드 URL.

    Sideload APK 배포에서 클라이언트가 강제 업데이트 여부를 판단할 때 사용.
    등록 릴리스가 없으면 모든 필드 None → 클라이언트는 enforcement 없음으로 해석.
    """
    channel = app_version_service.attendance_channel()
    latest, min_version = await app_version_service.get_for_channel(db, channel)
    if latest is None:
        return AppVersionResponse()
    return AppVersionResponse(
        min_version=min_version,
        latest_version=latest.version,
        download_url=app_version_service.presigned_download_url(latest.s3_key),
        release_notes=latest.release_notes,
    )
