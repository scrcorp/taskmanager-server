"""관리자 스토리지 라우터 — presigned URL 생성 + 로컬 업로드 API.

Admin Storage Router — Generates presigned URLs for S3 or local uploads.
로컬 모드에서는 PUT 엔드포인트로 파일을 직접 받아 저장합니다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.models.user import User
from app.services.storage_service import storage_service

router: APIRouter = APIRouter()


class PresignedUrlRequest(BaseModel):
    filename: str
    content_type: str
    folder: str = "reviews"


class PresignedUrlResponse(BaseModel):
    upload_url: str
    file_url: str


@router.post("/presigned-url", response_model=PresignedUrlResponse)
async def create_presigned_url(
    data: PresignedUrlRequest,
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """presigned upload URL을 생성합니다 (S3 또는 로컬)."""
    result = storage_service.generate_presigned_upload_url(
        filename=data.filename,
        content_type=data.content_type,
        folder=data.folder,
    )
    return {"upload_url": result["upload_url"], "file_url": result["file_url"]}


@router.put("/upload/{key:path}")
async def upload_local(
    key: str,
    request: Request,
) -> dict:
    """로컬 모드 전용 — 파일을 서버에 직접 저장합니다.

    Admin의 ImageUpload가 presigned URL 대신 이 엔드포인트로 PUT합니다.
    인증 없음 (presigned URL 발급 시 이미 인증됨).
    """
    body = await request.body()
    storage_service.save_local(key, body)
    return {"ok": True}
