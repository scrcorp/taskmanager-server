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
from app.services.storage_service import (
    InvalidStorageFolder,
    UnsafeStorageKey,
    storage_service,
)

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
        result = storage_service.presign_upload(
            folder=data.folder,
            filename=data.filename,
            content_type=data.content_type,
            base_url=base_url,
            upload_path_prefix="/api/v1/app/storage",
        )
    except InvalidStorageFolder:
        raise HTTPException(status_code=400, detail={"code": "invalid_folder"})
    except Exception as e:
        logger.error("Presigned URL 생성 실패: %s", e)
        raise HTTPException(
            status_code=503,
            detail="Storage service unavailable. Check AWS credentials.",
        )
    return {"upload_url": result["upload_url"], "file_url": result["file_url"]}


@router.put("/upload/{key:path}")
async def upload_local(
    key: str,
    request: Request,
) -> dict:
    """로컬 모드 전용 — 파일을 서버에 직접 저장합니다 (raw bytes PUT).

    키 안전성 검증(temp/ 한정 + traversal 차단)으로 임의경로 쓰기를 막는다.
    prod는 S3 직업로드라 이 엔드포인트를 타지 않는다(로컬 전용).

    NOTE(보류): 클라(console fetch)가 이 PUT에 Authorization 헤더를 싣지 않고,
    prod presigned는 S3 URL이라 헤더 추가 시 서명 충돌 위험. 그래서 인증은 키
    안전성 검증으로 대체. 토큰 기반 인증은 클라 동시 수정 시 Phase 2에서 검토.
    """
    body = await request.body()
    try:
        storage_service.receive_upload(key, body)
    except UnsafeStorageKey:
        raise HTTPException(status_code=400, detail={"code": "invalid_key"})
    return {"ok": True}
