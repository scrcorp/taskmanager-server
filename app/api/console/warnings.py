"""관리자 경고 라우터 — Warning v1 API.

Admin Warning Router — `/api/v1/console/warnings`.

Routing order: 정적 경로(/warnable-users, /counts)를 동적 /{warning_id} 보다
먼저 등록해야 shadow 되지 않는다 (evaluations.py 패턴).

Permission Matrix (warnings:* 는 GM 이상에 기본 부여):
    - 조회(목록/상세/카운트): warnings:read
    - 발행/picker: warnings:create (방향 검증 — 발행자보다 낮은 권한만)
    - 수정/해결: warnings:update (소유권 — Owner 전체 / GM 본인)
    - 삭제(소프트): warnings:delete (소유권 동일)

Store scoping:
    - POST/PUT: check_store_access (불가 → 403)
    - GET /: store_id 필터를 accessible 과 intersect (불가 매장 → 빈 페이지)
    - GET /{id}: 경고의 store 접근 가능 / Owner / issuer 본인만 (아니면 404)
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    check_store_access,
    get_accessible_store_ids,
    get_current_user,
    get_user_permissions,
    require_permission,
)
from app.core.permissions import is_owner
from app.database import get_db
from app.models.organization import Store
from app.models.user import User
from app.repositories.warning_category_repository import warning_category_repository
from app.schemas.common import MessageResponse, PaginatedResponse
from app.schemas.warning import (
    SavedSignatureResponse,
    SavedSignatureUpdate,
    WarnableUsersPage,
    WarningCountItem,
    WarningCreate,
    WarningMethodSwitchRequest,
    WarningResponse,
    WarningSignRequest,
    WarningUpdate,
)
from app.services.storage_service import storage_service
from app.services.warning_pdf_service import warning_pdf_service
from app.services.warning_service import warning_service
from app.services.warning_signature_service import (
    PARTY_MANAGER,
    warning_signature_service,
)
from app.utils.exceptions import BadRequestError, NotFoundError

# wet 서명 PDF 업로드 상한 (hiring 첨부와 동일 20MB).
MAX_WARNING_PDF_BYTES = 20 * 1024 * 1024

router: APIRouter = APIRouter()


# ====================================================================
# 정적 경로 — /{warning_id} 보다 먼저 등록 (shadow 방지)
# ====================================================================


@router.get("/warnable-users", response_model=WarnableUsersPage)
async def list_warnable_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:create"))],
    store_id: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    page: int = 1,
    limit: int = 30,
) -> dict:
    """경고 대상 직원 picker — 방향 필터(엄격히 낮은 권한) + 매장 스코프 + 검색/페이지.

    store_id 가 주어지면 그 매장 접근 가능 여부를 먼저 검증(불가 → 403).
    각 후보는 stores[] 에 자신의 모든 매장을 포함한다(store dropdown 제한).
    """
    page = max(1, page)
    limit = max(1, min(limit, 100))
    store_uuid: UUID | None = UUID(store_id) if store_id else None
    if store_uuid is not None:
        await check_store_access(db, current_user, store_uuid)
    return await warning_service.list_warnable_users(
        db, current_user, store_id=store_uuid, q=q, page=page, limit=limit
    )


@router.get("/counts", response_model=list[WarningCountItem])
async def warning_counts(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:read"))],
) -> list[dict]:
    """직원별 경고 갯수 (total/active) — Staff 목록 Warnings 칼럼용.

    store-scope: Owner 전체, GM 관리매장 한정. 갯수 0인 직원은 결과에 없음.
    """
    accessible = await get_accessible_store_ids(db, current_user)
    store_ids = list(accessible) if accessible is not None else None
    return await warning_service.get_counts(
        db, current_user.organization_id, store_ids=store_ids
    )


@router.get("/my-signature", response_model=SavedSignatureResponse)
async def get_my_signature(
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """콘솔 사용자(매니저) 본인의 저장 서명 조회 — manager 서명 재사용용.

    users.signature_strokes (employee/app 와 동일 컬럼). 없으면 signature=None.
    """
    return {"signature": warning_signature_service.get_saved_signature(current_user)}


@router.put("/my-signature", response_model=SavedSignatureResponse)
async def set_my_signature(
    data: SavedSignatureUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """콘솔 사용자(매니저) 본인의 저장 서명 설정/갱신."""
    saved = await warning_signature_service.set_saved_signature(
        db, current_user, data.to_strokes_payload()
    )
    return {"signature": saved}


# ====================================================================
# 경고 CRUD
# ====================================================================


@router.get("/", response_model=PaginatedResponse)
async def list_warnings(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:read"))],
    store_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    subject_user_id: Annotated[str | None, Query()] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """경고 목록 — org-scope, soft-delete 제외, created_at DESC.

    store_id 필터는 accessible 과 intersect (불가 매장 → 빈 페이지).
    subject_user_id 로 특정 직원 경고만 조회(Staff 상세 하단 이력).
    """
    per_page = max(1, min(per_page, 100))
    page = max(1, page)

    accessible = await get_accessible_store_ids(db, current_user)
    store_uuid: UUID | None = UUID(store_id) if store_id else None

    if store_uuid is not None:
        if accessible is not None and store_uuid not in accessible:
            return {"items": [], "total": 0, "page": page, "per_page": per_page}
        store_ids: list[UUID] | None = [store_uuid]
    else:
        store_ids = list(accessible) if accessible is not None else None

    warnings, total = await warning_service.list_warnings(
        db,
        organization_id=current_user.organization_id,
        store_ids=store_ids,
        status=status,
        category=category,
        subject_user_id=UUID(subject_user_id) if subject_user_id else None,
        page=page,
        per_page=per_page,
    )
    items = [await warning_service.build_warning_response(db, w) for w in warnings]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/{warning_id}", response_model=WarningResponse)
async def get_warning(
    warning_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:read"))],
) -> dict:
    """경고 상세. org 밖/soft-deleted/부재 시 404.

    추가로 경고의 store 가 접근 불가 + Owner 아님 + issuer 본인 아님이면 404
    (cross-store 존재 누설 방지).
    """
    warning = await warning_service.get_warning(
        db, warning_id=warning_id, organization_id=current_user.organization_id
    )

    if not is_owner(current_user) and warning.issued_by_id != current_user.id:
        accessible = await get_accessible_store_ids(db, current_user)
        if (
            accessible is not None
            and warning.store_id is not None
            and warning.store_id not in accessible
        ):
            raise NotFoundError("Warning not found")

    return await warning_service.build_warning_response(db, warning, include_ordinal=True)


@router.post("/{warning_id}/sign", response_model=WarningResponse)
async def sign_warning_as_manager(
    warning_id: UUID,
    data: WarningSignRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:read"))],
) -> dict:
    """경고에 manager 서명 적용 (party='manager').

    Identity gate (대리 금지 — 권한/priority 체크 아님):
        service 가 signer == warning.issued_by_id 를 강제한다. 따라서 발행자가
        아닌 GM 도, 발행자가 아닌 Owner/super-owner 도 403. (Owner 는 발행자를
        reassign 할 수 있을 뿐, 남의 이름으로 서명할 수는 없다.)

    active 경고만 서명 가능. 부재/org 밖/soft-deleted → 404.
    """
    warning = await warning_service.get_warning(
        db, warning_id=warning_id, organization_id=current_user.organization_id
    )
    await warning_signature_service.sign(
        db,
        warning=warning,
        party=PARTY_MANAGER,
        signer=current_user,
        strokes_payload=data.to_strokes_payload(),
        method=data.method,
        save_as_default=data.save_as_default,
    )
    fresh = await warning_service.get_warning(
        db, warning_id=warning_id, organization_id=current_user.organization_id
    )
    return await warning_service.build_warning_response(db, fresh, include_ordinal=True)


@router.put("/{warning_id}/method", response_model=WarningResponse)
async def switch_warning_method(
    warning_id: UUID,
    data: WarningMethodSwitchRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:update"))],
) -> dict:
    """서명 방식 전환 (digital↔wet).

    기존 서명/PDF 가 있으면 무효화되고(벡터 서명행 삭제 + PDF key 클리어, 파일은 보존)
    재서명 대기로 리셋된다. wet→digital 은 직원에게 재서명 알림. 전환은 철회 아님.
    """

    async def _check_store_access(store_id: UUID) -> None:
        await check_store_access(db, current_user, store_id)

    warning = await warning_service.switch_method(
        db,
        warning_id=warning_id,
        organization_id=current_user.organization_id,
        new_method=data.method,
        check_store_access=_check_store_access,
    )
    return await warning_service.build_warning_response(db, warning, include_ordinal=True)


@router.post("/{warning_id}/signed-pdf", response_model=WarningResponse)
async def upload_signed_pdf(
    warning_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:update"))],
    file: Annotated[UploadFile, File()],
    signed_on: Annotated[str | None, Form()] = None,
) -> dict:
    """wet 서명 PDF 업로드 = 서명완료.

    권한: 발행 매니저 본인은 본인 발행 건만, 오너/`warnings:upload` 는 타인 발행 건도.
    PDF 만(20MB 상한 + 매직바이트). signed_on(YYYY-MM-DD)=문서상 서명일(파일명 날짜).
    """
    if file.content_type not in ("application/pdf", "application/x-pdf"):
        raise BadRequestError("Only PDF files are allowed")
    pdf_bytes = await file.read()
    if len(pdf_bytes) > MAX_WARNING_PDF_BYTES:
        raise BadRequestError("File too large (max 20MB)")

    # 2-tier 권한 — 타인 발행 건 업로드 = 오너 OR warnings:upload.
    perms = await get_user_permissions(db, current_user.role_id)
    can_upload_others = is_owner(current_user) or ("warnings:upload" in perms)

    parsed_signed_on: date | None = None
    if signed_on:
        try:
            parsed_signed_on = date.fromisoformat(signed_on)
        except ValueError:
            raise BadRequestError("signed_on must be YYYY-MM-DD")

    async def _check_store_access(store_id: UUID) -> None:
        await check_store_access(db, current_user, store_id)

    warning = await warning_service.upload_wet_pdf(
        db,
        warning_id=warning_id,
        organization_id=current_user.organization_id,
        uploader=current_user,
        can_upload_others=can_upload_others,
        pdf_bytes=pdf_bytes,
        filename=file.filename or "signed.pdf",
        signed_on=parsed_signed_on,
        check_store_access=_check_store_access,
    )
    return await warning_service.build_warning_response(db, warning, include_ordinal=True)


@router.get("/{warning_id}/signed-pdf")
async def download_signed_pdf(
    warning_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:read"))],
) -> Response:
    """wet 서명 PDF 다운로드 — 권한 + store-scope 검증 후 표시용 파일명으로 서빙.

    응답 본문에 공개 URL 을 노출하지 않고(IDOR 방지) 인증된 엔드포인트가 직접 바이트 서빙.
    """
    warning = await warning_service.get_warning(
        db, warning_id=warning_id, organization_id=current_user.organization_id
    )
    # store-scope (get_warning 상세와 동일 IDOR 방지).
    if not is_owner(current_user) and warning.issued_by_id != current_user.id:
        accessible = await get_accessible_store_ids(db, current_user)
        if (
            accessible is not None
            and warning.store_id is not None
            and warning.store_id not in accessible
        ):
            raise NotFoundError("Warning not found")
    if not warning.signed_pdf_key:
        raise NotFoundError("No signed PDF for this warning")
    pdf_bytes = storage_service.read_bytes(warning.signed_pdf_key)
    if pdf_bytes is None:
        raise NotFoundError("Signed PDF file not found")

    subject = await db.get(User, warning.subject_user_id) if warning.subject_user_id else None
    store = await db.get(Store, warning.store_id) if warning.store_id else None
    labels = await warning_category_repository.labels_by_code(db, warning.organization_id)
    filename = warning_service.build_warning_filename(
        warning,
        subject_name=subject.full_name if subject else None,
        employee_no=subject.employee_no if subject else None,
        store_code=store.code if store else None,
        category_labels=labels,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{warning_id}/pdf")
async def download_warning_pdf(
    warning_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:read"))],
) -> Response:
    """경고 문서 PDF — 콘솔 폼(WarningFormDoc)을 서버가 그대로 렌더(페이지 분할 가능).

    digital: 이 PDF 가 곧 문서. wet: 이 PDF 를 출력→서명→스캔 업로드(스캔은
    /signed-pdf 로 별도 보관). 권한 + store-scope IDOR 가드는 signed-pdf 와 동일.
    """
    warning = await warning_service.get_warning(
        db, warning_id=warning_id, organization_id=current_user.organization_id
    )
    if not is_owner(current_user) and warning.issued_by_id != current_user.id:
        accessible = await get_accessible_store_ids(db, current_user)
        if (
            accessible is not None
            and warning.store_id is not None
            and warning.store_id not in accessible
        ):
            raise NotFoundError("Warning not found")

    labels = await warning_category_repository.labels_by_code(db, warning.organization_id)
    data = await warning_service.build_warning_response(
        db, warning, include_ordinal=True, category_labels=labels
    )
    # 사유 체크리스트용 — org 활성 카테고리 옵션(폼처럼 전체 표시 + 선택 체크).
    options = await warning_category_repository.list_for_org(
        db, warning.organization_id, include_hidden=False
    )
    categories = [{"code": c.code, "label": c.label} for c in options]
    # WeasyPrint 는 CPU-bound(sync) — 이벤트루프 안 막게 threadpool 로.
    pdf_bytes = await run_in_threadpool(warning_pdf_service.render_pdf, data, categories)

    filename = warning_service.build_warning_filename(
        warning,
        subject_name=data["subject_name"],
        employee_no=data["employee_no"],
        store_code=data["store_code"],
        category_labels=labels,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/", response_model=WarningResponse, status_code=201)
async def create_warning(
    data: WarningCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:create"))],
) -> dict:
    """새 경고 발행. 매장 접근 검증 + 방향 검증(상위→하위) + subject-store 검증."""
    await check_store_access(db, current_user, UUID(data.store_id))
    warning = await warning_service.create_warning(
        db,
        organization_id=current_user.organization_id,
        issuer=current_user,
        data=data,
    )
    return await warning_service.build_warning_response(db, warning)


@router.put("/{warning_id}", response_model=WarningResponse)
async def update_warning(
    warning_id: UUID,
    data: WarningUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:update"))],
) -> dict:
    """경고 수정/해결. 소유권(Owner 전체 / GM 본인) + (store 변경 시)매장 재검증."""

    async def _check_store_access(store_id: UUID) -> None:
        await check_store_access(db, current_user, store_id)

    warning = await warning_service.update_warning(
        db,
        warning_id=warning_id,
        organization_id=current_user.organization_id,
        current_user=current_user,
        data=data,
        check_store_access=_check_store_access,
    )
    return await warning_service.build_warning_response(db, warning)


@router.delete("/{warning_id}", response_model=MessageResponse)
async def delete_warning(
    warning_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("warnings:delete"))],
) -> dict:
    """경고 소프트 삭제. 소유권 검증. 이미 삭제/부재면 404 (idempotent-safe)."""
    await warning_service.delete_warning(
        db,
        warning_id=warning_id,
        organization_id=current_user.organization_id,
        current_user=current_user,
    )
    return {"message": "Warning deleted"}
