"""앱 경고 라우터 — 직원 본인 경고 조회 + 확인(자동) + 서명 API.

App Warning Router — `/api/v1/app/my/warnings`.

대상 직원(subject) 본인만 접근. org/self scope — 다른 사람 경고는 404.

핵심 동작:
    - GET /{id} 상세 열람 시 acknowledged_at 자동 stamp (확인 != 서명).
    - POST /{id}/sign 은 employee party 서명 (signer=current_user).
      service 가 signer == subject 강제 (대리 금지).
    - 저장 서명(users.signature_strokes)은 employee/manager 공용 재사용 템플릿.

Routing order: 정적 경로(/unsigned-count, /saved-signature)를 동적 /{warning_id}
보다 먼저 등록해야 shadow 되지 않는다.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.organization import Store
from app.models.user import User
from app.repositories.warning_category_repository import warning_category_repository
from app.schemas.common import PaginatedResponse
from app.schemas.warning import (
    SavedSignatureResponse,
    SavedSignatureUpdate,
    WarningResponse,
    WarningSignRequest,
)
from app.services.storage_service import storage_service
from app.services.warning_service import warning_service
from app.services.warning_signature_service import (
    PARTY_EMPLOYEE,
    warning_signature_service,
)
from app.utils.exceptions import NotFoundError

router: APIRouter = APIRouter()


# ====================================================================
# 정적 경로 — /{warning_id} 보다 먼저 등록 (shadow 방지)
# ====================================================================


@router.get("", response_model=PaginatedResponse)
async def list_my_warnings(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """내 active 경고 목록 + 서명 상태. withdrawn/삭제 제외."""
    per_page = max(1, min(per_page, 100))
    page = max(1, page)
    warnings, total = await warning_service.list_my_warnings(
        db,
        organization_id=current_user.organization_id,
        subject_user_id=current_user.id,
        page=page,
        per_page=per_page,
    )
    items = [
        await warning_service.build_warning_response(db, w) for w in warnings
    ]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/unsigned-count")
async def my_unsigned_count(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """내 active 경고 중 미서명(employee 서명 없음) 갯수 — badge 용."""
    count = await warning_service.count_my_unsigned(
        db,
        organization_id=current_user.organization_id,
        subject_user_id=current_user.id,
    )
    return {"unsigned_count": count}


@router.get("/saved-signature", response_model=SavedSignatureResponse)
async def get_my_saved_signature(
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """내 저장 서명(users.signature_strokes) 조회. 없으면 signature=None."""
    return {"signature": warning_signature_service.get_saved_signature(current_user)}


@router.put("/saved-signature", response_model=SavedSignatureResponse)
async def set_my_saved_signature(
    data: SavedSignatureUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """내 저장 서명 설정/갱신."""
    saved = await warning_signature_service.set_saved_signature(
        db, current_user, data.to_strokes_payload()
    )
    return {"signature": saved}


# ====================================================================
# 동적 경로 — 상세 / 서명
# ====================================================================


@router.get("/{warning_id}", response_model=WarningResponse)
async def get_my_warning(
    warning_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """내 경고 상세. 본인 소유 아니면 404. **여기서 acknowledged_at 자동 stamp.**"""
    warning = await warning_service.get_my_warning(
        db,
        warning_id=warning_id,
        organization_id=current_user.organization_id,
        subject_user_id=current_user.id,
    )
    # 자동 확인 — 직원이 상세를 처음 열면 acknowledged_at 최초 1회 stamp.
    warning = await warning_service.acknowledge_warning(db, warning)
    return await warning_service.build_warning_response(
        db, warning, include_ordinal=True
    )


@router.get("/{warning_id}/signed-pdf")
async def download_my_signed_pdf(
    warning_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    """내 wet 서명 PDF 다운로드 — 본인 소유만(아니면 404), 표시용 파일명으로 서빙."""
    warning = await warning_service.get_my_warning(
        db,
        warning_id=warning_id,
        organization_id=current_user.organization_id,
        subject_user_id=current_user.id,
    )
    if not warning.signed_pdf_key:
        raise NotFoundError("No signed PDF for this warning")
    pdf_bytes = storage_service.read_bytes(warning.signed_pdf_key)
    if pdf_bytes is None:
        raise NotFoundError("Signed PDF file not found")
    store = await db.get(Store, warning.store_id) if warning.store_id else None
    labels = await warning_category_repository.labels_by_code(db, warning.organization_id)
    filename = warning_service.build_warning_filename(
        warning,
        subject_name=current_user.full_name,
        employee_no=current_user.employee_no,
        store_code=store.code if store else None,
        category_labels=labels,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{warning_id}/sign", response_model=WarningResponse)
async def sign_my_warning(
    warning_id: UUID,
    data: WarningSignRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """내 경고에 employee 서명 적용 (signer=current_user, party='employee').

    service 가 signer == subject_user_id 강제 (대리 금지). active 경고만.
    """
    warning = await warning_service.get_my_warning(
        db,
        warning_id=warning_id,
        organization_id=current_user.organization_id,
        subject_user_id=current_user.id,
    )
    await warning_signature_service.sign(
        db,
        warning=warning,
        party=PARTY_EMPLOYEE,
        signer=current_user,
        strokes_payload=data.to_strokes_payload(),
        method=data.method,
        save_as_default=data.save_as_default,
    )
    # 갱신된 상세 반환 (서명 반영).
    fresh = await warning_service.get_my_warning(
        db,
        warning_id=warning_id,
        organization_id=current_user.organization_id,
        subject_user_id=current_user.id,
    )
    return await warning_service.build_warning_response(
        db, fresh, include_ordinal=True
    )
