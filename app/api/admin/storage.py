"""관리자 스토리지 라우터 — S3 presigned URL 생성 API.

Admin Storage Router — Generates presigned URLs for direct S3 uploads.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
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
    """S3 presigned upload URL을 생성합니다.

    Generate a presigned URL for direct browser-to-S3 upload.
    """
    result = storage_service.generate_presigned_upload_url(
        filename=data.filename,
        content_type=data.content_type,
        folder=data.folder,
    )
    return {"upload_url": result["upload_url"], "file_url": result["file_url"]}
