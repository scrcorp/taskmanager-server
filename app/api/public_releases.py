"""Public releases 라우터 — 인증 없이 호출 가능한 다운로드 메타 endpoint.

`hermesops.site/htma-download` 같은 매장 staff 공유용 단축 URL 이 호출.
APK 자체 보안은 access code (device 등록 시) 에서 처리하므로 다운로드 메타는 public.
"""

from fastapi import APIRouter, HTTPException, status

from app.schemas.app_version import AppVersionLatestResponse
from app.services.app_version_service import app_version_service


router: APIRouter = APIRouter()


@router.get("/releases/attendance/latest", response_model=AppVersionLatestResponse)
def get_attendance_latest_public() -> AppVersionLatestResponse:
    """현재 서버 환경 bucket 의 attendance APK 들 중 버전 가장 높은 것 반환.

    No auth — 매장 staff 가 매니저로부터 받은 단축 URL 로 직접 다운로드.
    응답은 console/app_versions.py 의 동일 endpoint 와 같은 schema.
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
