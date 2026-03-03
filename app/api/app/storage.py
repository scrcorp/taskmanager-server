"""앱 스토리지 라우터 — presigned URL 생성 + 로컬/S3 업로드 API.

App Storage Router — Generates presigned URLs for S3 or local uploads.
앱(직원용)에서 체크리스트 사진 등을 업로드할 때 사용합니다.
기본 폴더는 "completions"입니다.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.models.user import User
from app.services.storage_service import storage_service

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter()


class PresignedUrlRequest(BaseModel):
    filename: str
    content_type: str
    folder: str = "completions"


class PresignedUrlResponse(BaseModel):
    upload_url: str
    file_url: str


@router.post("/presigned-url", response_model=PresignedUrlResponse)
async def create_presigned_url(
    data: PresignedUrlRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """presigned upload URL을 생성합니다 (S3 또는 로컬)."""
    base_url = str(request.base_url).rstrip("/")
    try:
        result = storage_service.generate_presigned_upload_url(
            filename=data.filename,
            content_type=data.content_type,
            folder=data.folder,
            base_url=base_url,
            upload_path_prefix="/api/v1/app/storage",
        )
    except Exception as e:
        logger.error("Presigned URL 생성 실패: %s", e)
        raise HTTPException(
            status_code=503,
            detail="파일 업로드 서비스를 사용할 수 없습니다. AWS 자격증명을 확인해주세요. (Storage service unavailable. Check AWS credentials.)",
        )
    return {"upload_url": result["upload_url"], "file_url": result["file_url"]}


@router.put("/upload/{key:path}")
async def upload_local(
    key: str,
    request: Request,
) -> dict:
    """로컬 모드 전용 — 파일을 서버에 직접 저장합니다 (raw bytes PUT).

    S3 presigned URL과 동일한 방식. 인증 없음.
    """
    body = await request.body()
    storage_service.save_local(key, body)
    return {"ok": True}
