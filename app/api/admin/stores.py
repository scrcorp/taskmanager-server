"""관리자 매장 라우터 — 매장 CRUD 엔드포인트.

Admin Store Router — CRUD endpoints for store management.
All endpoints are scoped to the current organization from JWT.

Permission Matrix (역할별 권한 설계):
    - 매장 등록/수정/삭제: Owner만
    - 매장 목록/상세 조회: Owner 전체, GM 담당 매장, SV 소속 매장
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    check_store_access,
    get_accessible_store_ids,
    hide_cost_for,
    require_permission,
    scrub_cost_fields,
)
from app.database import get_db
from app.models.user import User
from app.schemas.organization import (
    StoreCreate,
    StoreDetailResponse,
    StoreResponse,
    StoreUpdate,
)
from app.services.store_service import store_service

router: APIRouter = APIRouter()


@router.get("", response_model=list[StoreResponse])
async def list_stores(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:read"))],
) -> list[StoreResponse]:
    """매장 목록을 조회합니다. Owner=전체, GM=담당 매장, SV=소속 매장.

    List stores scoped to user's accessible stores.
    """
    org_id: UUID = current_user.organization_id
    accessible = await get_accessible_store_ids(db, current_user)
    stores = await store_service.list_stores(db, org_id, accessible_store_ids=accessible)
    if hide_cost_for(current_user):
        for s in stores:
            scrub_cost_fields(s)
    return stores


@router.get("/{store_id}", response_model=StoreDetailResponse)
async def get_store(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:read"))],
) -> StoreDetailResponse:
    """매장 상세 정보를 조회합니다 (근무조/직책 포함). 담당 매장만 접근 가능.

    Retrieve store detail with shifts and positions. Scoped to accessible stores.
    """
    await check_store_access(db, current_user, store_id)
    org_id: UUID = current_user.organization_id
    store = await store_service.get_store(db, store_id, org_id)
    if hide_cost_for(current_user):
        scrub_cost_fields(store)
    return store


@router.post("", response_model=StoreResponse, status_code=201)
async def create_store(
    data: StoreCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:create"))],
) -> StoreResponse:
    """새 매장을 생성합니다. Owner만 가능.

    Create a new store in the current organization. Owner only.
    """
    org_id: UUID = current_user.organization_id
    store = await store_service.create_store(db, org_id, data)
    if hide_cost_for(current_user):
        scrub_cost_fields(store)
    return store


@router.put("/{store_id}", response_model=StoreResponse)
async def update_store(
    store_id: UUID,
    data: StoreUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> StoreResponse:
    """매장 정보를 수정합니다. Owner만 가능.

    Update an existing store. Owner only.
    """
    org_id: UUID = current_user.organization_id
    store = await store_service.update_store(db, store_id, org_id, data)
    if hide_cost_for(current_user):
        scrub_cost_fields(store)
    return store


@router.delete("/{store_id}", status_code=204)
async def delete_store(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:delete"))],
) -> None:
    """매장을 삭제합니다. Owner만 가능.

    Delete a store by its ID. Owner only.
    """
    org_id: UUID = current_user.organization_id
    await store_service.delete_store(db, store_id, org_id)


@router.get("/{store_id}/work-date")
async def get_store_work_date(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:read"))],
) -> dict:
    """매장의 현재 work_date를 경계 시각 기준으로 반환합니다.

    Get the current work_date for a store based on its day boundary config.
    """
    await check_store_access(db, current_user, store_id)
    from app.utils.timezone import get_store_day_config, get_work_date
    store_tz, day_start = await get_store_day_config(db, store_id)
    work_date: date = get_work_date(store_tz, day_start)
    return {
        "store_id": str(store_id),
        "work_date": str(work_date),
        "timezone": store_tz,
        "day_start_time": day_start,
    }


# ============================================================
# Hiring — accepting signups + cover photos
# ============================================================

from datetime import datetime, timezone as _dt_tz
from uuid import uuid4

from fastapi import File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.models.organization import Store
from app.services.storage_service import storage_service


# 허용 이미지 포맷 — 서버 측 magic byte 검증은 우선 ContentType + 확장자만
_ALLOWED_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_PHOTO_BYTES = 5 * 1024 * 1024  # 5 MB


class _AcceptingSignupsBody(BaseModel):
    accepting_signups: bool


class _CoverPhotoOut(BaseModel):
    id: str
    url: str | None
    is_primary: bool
    uploaded_at: str
    size: int


def _photos_to_response(photos: list[dict]) -> list[dict]:
    out: list[dict] = []
    for photo in photos or []:
        url = storage_service.resolve_url(photo.get("key"))
        out.append({
            "id": photo.get("id", ""),
            "url": url,
            "is_primary": bool(photo.get("is_primary", False)),
            "uploaded_at": photo.get("uploaded_at", ""),
            "size": int(photo.get("size", 0)),
        })
    return out


async def _load_store_for_hiring(db: AsyncSession, store_id: UUID, org_id: UUID) -> Store:
    result = await db.execute(
        select(Store).where(Store.id == store_id, Store.organization_id == org_id)
    )
    store = result.scalar_one_or_none()
    if store is None or store.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "store_not_found"})
    return store


@router.patch("/{store_id}/accepting-signups")
async def set_accepting_signups(
    store_id: UUID,
    body: _AcceptingSignupsBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> dict:
    """공개 가입 링크 활성/일시정지 토글. Owner/GM만."""
    await check_store_access(db, current_user, store_id)
    store = await _load_store_for_hiring(db, store_id, current_user.organization_id)
    store.accepting_signups = body.accepting_signups
    await db.commit()
    return {"store_id": str(store_id), "accepting_signups": store.accepting_signups}


@router.get("/{store_id}/cover-photos")
async def list_cover_photos(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:read"))],
) -> list[dict]:
    """매장 표지 사진 목록."""
    await check_store_access(db, current_user, store_id)
    store = await _load_store_for_hiring(db, store_id, current_user.organization_id)
    return _photos_to_response(store.cover_photos or [])


@router.post("/{store_id}/cover-photos", status_code=201)
async def upload_cover_photo(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
    file: UploadFile = File(...),
    set_as_primary: bool = Form(False),
) -> dict:
    """매장 표지 사진 업로드. 첫 사진은 자동 primary."""
    await check_store_access(db, current_user, store_id)

    if file.content_type not in _ALLOWED_PHOTO_TYPES:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_file_type", "message": "Only JPG, PNG, WebP allowed."},
        )

    data = await file.read()
    if len(data) > _MAX_PHOTO_BYTES:
        raise HTTPException(
            status_code=400,
            detail={"code": "file_too_large", "message": "Max 5 MB."},
        )

    store = await _load_store_for_hiring(db, store_id, current_user.organization_id)
    photos = list(store.cover_photos or [])

    key = storage_service.upload_bytes(
        data, filename=file.filename or "photo.jpg",
        folder="store_covers", content_type=file.content_type,
    )
    photo_id = uuid4().hex[:12]
    is_primary = set_as_primary or not photos
    if is_primary:
        for p in photos:
            p["is_primary"] = False

    photo = {
        "id": photo_id,
        "key": key,
        "is_primary": is_primary,
        "uploaded_at": datetime.now(_dt_tz.utc).isoformat(),
        "size": len(data),
    }
    photos.append(photo)
    store.cover_photos = photos
    flag_modified(store, "cover_photos")
    await db.commit()

    return _photos_to_response([photo])[0]


@router.patch("/{store_id}/cover-photos/{photo_id}/primary")
async def set_cover_photo_primary(
    store_id: UUID,
    photo_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> dict:
    """primary 사진 변경."""
    await check_store_access(db, current_user, store_id)
    store = await _load_store_for_hiring(db, store_id, current_user.organization_id)

    photos = list(store.cover_photos or [])
    target = next((p for p in photos if p.get("id") == photo_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "photo_not_found"})

    for p in photos:
        p["is_primary"] = p.get("id") == photo_id
    store.cover_photos = photos
    flag_modified(store, "cover_photos")
    await db.commit()
    return {"id": photo_id, "is_primary": True}


@router.delete("/{store_id}/cover-photos/{photo_id}", status_code=204)
async def delete_cover_photo(
    store_id: UUID,
    photo_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("stores:update"))],
) -> None:
    """사진 삭제. primary였으면 첫 번째 남은 사진을 primary로 승격."""
    await check_store_access(db, current_user, store_id)
    store = await _load_store_for_hiring(db, store_id, current_user.organization_id)

    photos = list(store.cover_photos or [])
    target = next((p for p in photos if p.get("id") == photo_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "photo_not_found"})

    storage_service.delete_file(target.get("key", ""))
    photos = [p for p in photos if p.get("id") != photo_id]

    if target.get("is_primary") and photos:
        photos[0]["is_primary"] = True

    store.cover_photos = photos
    flag_modified(store, "cover_photos")
    await db.commit()
