"""연락처(Contacts) 라우터 — `/api/v1/console/contacts`.

계약: `docs/99_inbox/2026-08-14-연락처-API계약.md` §3
설계: `docs/99_inbox/2026-08-14-연락처(Contacts)-기능-설계.md` (D1~D9)

Routing order:
    정적 경로(/tags, /requests, /requests/mine)를 동적 /{contact_id} 보다 **먼저**
    등록해야 shadow 되지 않는다 (warnings.py 와 동일 규칙).

Collection 경로는 trailing slash 필수 (`GET /contacts/`, `POST /contacts/`).
prod 307 리다이렉트 → http 다운그레이드로 CORS 가 깨진 전례가 있다.

Permission Matrix:
    - 조회(목록/상세/태그/신청): contacts:read
    - 생성: contacts:create / 수정: contacts:update / 삭제: contacts:delete
    - 신청 생성·취소: contacts:read (본인 신청만 취소)
    - 신청 승인·반려: 신청 종류에 대응하는 쓰기 권한 (create→contacts:create ...)
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_accessible_store_ids, require_permission, user_has_permissions
from app.core.error_codes.contacts import CONTACT_PERMISSION_DENIED
from app.database import get_db
from app.models.user import User
from app.schemas.common import PaginatedResponse
from app.schemas.contact import (
    ContactApproveResponse,
    ContactBulkCreate,
    ContactBulkCreateResult,
    ContactBulkUpdate,
    ContactBulkUpdateResult,
    ContactChangeRequestCreate,
    ContactChangeRequestResponse,
    ContactCreate,
    ContactDeleteRequest,
    ContactDeleteResponse,
    ContactRequestApprove,
    ContactRequestReject,
    ContactResponse,
    ContactTagResponse,
    ContactUpdate,
    ContactVisibilityPreview,
)
from app.services.contact_service import REQUEST_TYPE_PERMISSION, contact_service

router: APIRouter = APIRouter()


# ====================================================================
# 정적 경로 — /{contact_id} 보다 먼저 등록 (shadow 방지)
# ====================================================================


@router.get("/tags", response_model=list[ContactTagResponse])
async def list_contact_tags(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:read"))],
    q: Annotated[str | None, Query(description="Prefix match on the tag key")] = None,
    limit: Annotated[int, Query()] = 20,
) -> list[ContactTagResponse]:
    """태그 자동완성 — org 단위 태그 마스터.

    usage_count 는 caller 가 볼 수 있는 연락처 기준이다(안 보이는 연락처 수를 노출하지 않음).
    """
    accessible = await get_accessible_store_ids(db, current_user)
    return await contact_service.list_tags(
        db, current_user, accessible, q=q, limit=limit
    )


@router.get("/requests/mine", response_model=PaginatedResponse)
async def list_my_contact_requests(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:read"))],
    request_status: Annotated[str, Query(alias="status")] = "all",
    page: Annotated[int, Query()] = 1,
    per_page: Annotated[int, Query()] = 20,
) -> dict:
    """내가 낸 변경 신청 목록 — 가시성 절 미적용 (N4: 매장이 바뀌어도 결과를 봐야 한다)."""
    accessible = await get_accessible_store_ids(db, current_user)
    items, total = await contact_service.list_my_requests(
        db, current_user, accessible, status=request_status, page=page, per_page=per_page
    )
    return {"items": items, "total": total, "page": max(1, page), "per_page": per_page}


@router.get("/requests", response_model=PaginatedResponse)
async def list_contact_requests(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:read"))],
    request_status: Annotated[str, Query(alias="status")] = "pending",
    request_type: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query()] = 1,
    per_page: Annotated[int, Query()] = 20,
) -> dict:
    """처리 대기 신청 목록 (승인자용).

    caller 가 처리할 수 있는 종류만 담는다. 쓰기 권한이 하나도 없으면 빈 페이지
    (403 이 아니라 빈 결과 — 직접 호출해도 정보가 새지 않게).
    """
    accessible = await get_accessible_store_ids(db, current_user)
    writable = await contact_service.writable_request_types(db, current_user)
    items, total = await contact_service.list_requests(
        db,
        current_user,
        accessible,
        writable_types=writable,
        status=request_status,
        request_type=request_type,
        page=page,
        per_page=per_page,
    )
    return {"items": items, "total": total, "page": max(1, page), "per_page": per_page}


@router.post(
    "/requests",
    response_model=ContactChangeRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_contact_request(
    data: ContactChangeRequestCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:read"))],
) -> ContactChangeRequestResponse:
    """변경 신청 생성 (등록/수정/삭제).

    쓰기 권한을 가진 사용자가 호출하면 대기시키지 않고 **그 자리에서 반영**한다
    (응답 status=approved). 신청은 쓰기 권한이 없는 열람자를 위한 경로다.
    """
    accessible = await get_accessible_store_ids(db, current_user)
    writable = await contact_service.writable_request_types(db, current_user)
    return await contact_service.create_request(
        db, current_user, accessible, data, writable_types=writable
    )


@router.post("/requests/{request_id}/cancel", response_model=ContactChangeRequestResponse)
async def cancel_contact_request(
    request_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:read"))],
) -> ContactChangeRequestResponse:
    """내 신청 취소 — 본인 + pending 만."""
    accessible = await get_accessible_store_ids(db, current_user)
    return await contact_service.cancel_request(
        db, current_user, accessible, request_id
    )


@router.post("/requests/{request_id}/approve", response_model=ContactApproveResponse)
async def approve_contact_request(
    request_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:read"))],
    body: Annotated[ContactRequestApprove, Body()] = ContactRequestApprove(),
) -> ContactApproveResponse:
    """신청 승인 + 반영. 처리 권한은 신청 종류별 쓰기 권한 (§1).

    stale(그 사이 원본이 바뀜)이어도 진행한다 — 경고만 하고 차단하지 않는다(N5).
    """
    await _require_request_permission(db, current_user, request_id)
    accessible = await get_accessible_store_ids(db, current_user)
    return await contact_service.approve_request(
        db, current_user, accessible, request_id, body
    )


@router.post("/requests/{request_id}/reject", response_model=ContactChangeRequestResponse)
async def reject_contact_request(
    request_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:read"))],
    # 본문/사유 누락은 서비스가 CONTACT_REASON_REQUIRED(400)로 답한다 (계약 §6).
    body: ContactRequestReject | None = Body(default=None),
) -> ContactChangeRequestResponse:
    """신청 반려 — 사유 필수. 처리 권한은 신청 종류별 쓰기 권한."""
    await _require_request_permission(db, current_user, request_id)
    accessible = await get_accessible_store_ids(db, current_user)
    return await contact_service.reject_request(
        db, current_user, accessible, request_id, (body.reason if body else None) or ""
    )


async def _require_request_permission(
    db: AsyncSession, current_user: User, request_id: UUID
) -> None:
    """신청 종류에 대응하는 쓰기 권한을 요구한다 (없으면 403).

    request 를 먼저 로드해야 종류를 알 수 있으므로 Depends 가 아닌 명시 호출이다.
    존재하지 않는 신청은 서비스가 404 로 답한다.
    """
    request_type = await contact_service.get_request_type(db, current_user, request_id)
    code = REQUEST_TYPE_PERMISSION[request_type]
    if not await user_has_permissions(db, current_user, code):
        raise CONTACT_PERMISSION_DENIED(required_permission=code)


# ====================================================================
# 동적 경로
# ====================================================================


@router.get("/{contact_id}", response_model=ContactResponse)
async def get_contact(
    contact_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:read"))],
) -> ContactResponse:
    """연락처 상세 — 가시성 절 불통과는 403 이 아니라 404(존재를 숨긴다)."""
    accessible = await get_accessible_store_ids(db, current_user)
    return await contact_service.get_contact(db, current_user, accessible, contact_id)


@router.put("/{contact_id}", response_model=ContactResponse)
async def update_contact(
    contact_id: UUID,
    data: ContactUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:update"))],
) -> ContactResponse:
    """연락처 수정 (전체 치환, 사유 필수). 보낸 키만 반영한다."""
    accessible = await get_accessible_store_ids(db, current_user)
    return await contact_service.update_contact_api(
        db, current_user, accessible, contact_id, data
    )


@router.delete("/{contact_id}", response_model=ContactDeleteResponse)
async def delete_contact(
    contact_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:delete"))],
    # 본문 누락도 422 가 아니라 서비스의 CONTACT_REASON_REQUIRED(400)로 답한다 (계약 §6).
    body: ContactDeleteRequest | None = Body(default=None),
) -> dict:
    """연락처 소프트 삭제 (본문에 사유 필수).

    대상의 pending 신청은 superseded 로 전환되며, 몇 건이 무효화됐는지 응답에 담는다
    (조용한 실패 금지).
    """
    accessible = await get_accessible_store_ids(db, current_user)
    return await contact_service.delete_contact(
        db, current_user, accessible, contact_id, (body.reason if body else None) or ""
    )


# ====================================================================
# Collection (trailing slash)
# ====================================================================


@router.put("/{contact_id}/favorite", response_model=ContactResponse)
async def add_favorite(
    contact_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:read"))],
) -> ContactResponse:
    """즐겨찾기 등록 — **멱등**. 이미 켜져 있어도 200 이다.

    쓰기 권한을 보지 않는 이유: 즐겨찾기는 연락처를 바꾸는 행위가 아니라
    **보는 사람의 개인 설정**이다. 읽을 수 있으면 자기 별은 달 수 있다.
    """
    accessible = await get_accessible_store_ids(db, current_user)
    return await contact_service.set_favorite(
        db, current_user, accessible, contact_id, True
    )


@router.delete("/{contact_id}/favorite", response_model=ContactResponse)
async def remove_favorite(
    contact_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:read"))],
) -> ContactResponse:
    """즐겨찾기 해제 — **멱등**. 없던 것을 지워도 200 이다."""
    accessible = await get_accessible_store_ids(db, current_user)
    return await contact_service.set_favorite(
        db, current_user, accessible, contact_id, False
    )


@router.post("/bulk", response_model=ContactBulkCreateResult)
async def bulk_create_contacts(
    data: ContactBulkCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:create"))],
) -> dict:
    """대량 등록 (D3/D4) — 기본은 `dry_run=true` 미리보기.

    **전부 되거나 전부 안 된다.** 한 행이라도 검증에 걸리면 아무것도 저장하지 않고
    어느 줄이 왜 실패했는지 돌려준다. 정적 경로라 `/{contact_id}` 보다 먼저 등록한다.
    """
    accessible = await get_accessible_store_ids(db, current_user)
    return await contact_service.bulk_create(db, current_user, accessible, data)


@router.post("/bulk-update", response_model=ContactBulkUpdateResult)
async def bulk_update_contacts(
    data: ContactBulkUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:update"))],
) -> dict:
    """일괄 수정 (D2) — 태그 추가/제거, 회사명, 가시성. 사유 필수.

    이력은 연락처마다 한 행 + 배치 id 로 묶인다 (D1).
    """
    accessible = await get_accessible_store_ids(db, current_user)
    return await contact_service.bulk_update(db, current_user, accessible, data)


@router.post("/visibility-preview")
async def preview_visibility(
    data: ContactVisibilityPreview,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:read"))],
) -> dict:
    """이 가시성 설정이면 **지금 누가 보는가** — 저장 전 명단 미리보기 (V4/V5).

    정적 경로라 `/{contact_id}` 보다 먼저 등록돼야 한다(shadow 방지).
    """
    return await contact_service.preview_viewers(
        db,
        current_user,
        data.visibility,
        data.targets or [],
        data.excluded_user_ids or [],
    )


@router.get("/", response_model=PaginatedResponse)
async def list_contacts(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:read"))],
    q: Annotated[str | None, Query(description="Search name, company, phone, email, memo, tag")] = None,
    tag: Annotated[str | None, Query()] = None,
    store_id: Annotated[str | None, Query(description="Store UUID or 'none' for all-store contacts")] = None,
    visibility: Annotated[str | None, Query(description="'organization' | 'restricted'")] = None,
    favorites_only: Annotated[bool, Query(description="Only contacts the caller starred")] = False,
    sort: Annotated[str, Query()] = "name",
    page: Annotated[int, Query()] = 1,
    per_page: Annotated[int, Query()] = 20,
) -> dict:
    """연락처 목록/검색 — 기본 이름순. **즐겨찾기는 어느 정렬에서든 맨 위로 온다** (D4).

    q 한 개로 이름/업체/요약/메모/태그/전화번호(원본·정규화)/이메일/링크를 OR 부분일치한다.
    """
    accessible = await get_accessible_store_ids(db, current_user)
    items, total = await contact_service.list_contacts(
        db,
        current_user,
        accessible,
        q=q,
        tag=tag,
        store_id=store_id,
        visibility=visibility,
        favorites_only=favorites_only,
        sort=sort,
        page=page,
        per_page=per_page,
    )
    return {"items": items, "total": total, "page": max(1, page), "per_page": per_page}


@router.post("/", response_model=ContactResponse, status_code=status.HTTP_201_CREATED)
async def create_contact(
    data: ContactCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("contacts:create"))],
) -> ContactResponse:
    """연락처 생성. 같은 번호가 이미 있으면 차단하지 않고 응답에 경고를 담는다(N7)."""
    accessible = await get_accessible_store_ids(db, current_user)
    return await contact_service.create_contact_api(
        db, current_user, accessible, data
    )
