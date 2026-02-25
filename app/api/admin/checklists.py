"""관리자 체크리스트 라우터 — 체크리스트 템플릿 및 항목 관리 API.

Admin Checklist Router — API endpoints for checklist template and item management.
Provides CRUD operations for templates and their items, including reordering.

Permission Matrix (역할별 권한 설계):
    - 체크리스트 생성/수정/삭제: Owner + GM (담당 매장)
    - 체크리스트 조회: Owner + GM + SV (소속 매장)
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import StreamingResponse
from io import BytesIO
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, get_accessible_store_ids, require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.common import (
    ChecklistBulkItemCreate,
    ChecklistItemCreate,
    ChecklistItemResponse,
    ChecklistItemUpdate,
    ChecklistTemplateCreate,
    ChecklistTemplateResponse,
    ChecklistTemplateUpdate,
    ExcelImportResponse,
    MessageResponse,
    ReorderRequest,
)
from app.services.checklist_service import checklist_service
from app.utils.exceptions import BadRequestError

router: APIRouter = APIRouter()


# === 템플릿 엔드포인트 (Template Endpoints) ===


@router.get(
    "/checklist-templates",
    response_model=list[ChecklistTemplateResponse],
)
async def list_all_templates(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:read"))],
    store_id: Annotated[str | None, Query()] = None,
    shift_id: Annotated[str | None, Query()] = None,
    position_id: Annotated[str | None, Query()] = None,
) -> list[dict]:
    """조직 전체의 체크리스트 템플릿 목록을 조회합니다. 접근 가능한 매장만 필터링.

    List all checklist templates for the organization with optional filters.
    Results are filtered to only include templates from accessible stores.
    """
    store_uuid: UUID | None = UUID(store_id) if store_id else None
    shift_uuid: UUID | None = UUID(shift_id) if shift_id else None
    position_uuid: UUID | None = UUID(position_id) if position_id else None

    accessible = await get_accessible_store_ids(db, current_user)

    # 특정 매장 필터가 있으면 접근 권한 확인 — Validate store filter against access scope
    if store_uuid is not None and accessible is not None and store_uuid not in accessible:
        return []

    templates = await checklist_service.list_all_templates(
        db,
        organization_id=current_user.organization_id,
        store_id=store_uuid,
        shift_id=shift_uuid,
        position_id=position_uuid,
    )

    # 접근 가능한 매장 템플릿만 필터링 — Filter to accessible stores
    if accessible is not None:
        accessible_set = set(accessible)
        templates = [t for t in templates if t.store_id in accessible_set]

    return [
        {
            "id": str(t.id),
            "store_id": str(t.store_id),
            "shift_id": str(t.shift_id),
            "position_id": str(t.position_id),
            "shift_name": t.shift.name if t.shift else "",
            "position_name": t.position.name if t.position else "",
            "title": t.title,
            "item_count": len(t.items) if t.items else 0,
        }
        for t in templates
    ]


@router.get(
    "/stores/{store_id}/checklist-templates",
    response_model=list[ChecklistTemplateResponse],
)
async def list_templates(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:read"))],
    shift_id: Annotated[str | None, Query()] = None,
    position_id: Annotated[str | None, Query()] = None,
) -> list[dict]:
    """매장별 체크리스트 템플릿 목록을 조회합니다. 담당/소속 매장만 접근 가능.

    List checklist templates for a store. Scoped to accessible stores.
    """
    await check_store_access(db, current_user, store_id)

    shift_uuid: UUID | None = UUID(shift_id) if shift_id else None
    position_uuid: UUID | None = UUID(position_id) if position_id else None

    templates = await checklist_service.list_templates(
        db,
        store_id=store_id,
        organization_id=current_user.organization_id,
        shift_id=shift_uuid,
        position_id=position_uuid,
    )

    return [
        {
            "id": str(t.id),
            "store_id": str(t.store_id),
            "shift_id": str(t.shift_id),
            "position_id": str(t.position_id),
            "shift_name": t.shift.name if t.shift else "",
            "position_name": t.position.name if t.position else "",
            "title": t.title,
            "item_count": len(t.items) if t.items else 0,
        }
        for t in templates
    ]


# === Excel Import/Export (must be registered BEFORE /{template_id}) ===


@router.post(
    "/checklist-templates/import",
    response_model=ExcelImportResponse,
)
async def import_from_excel(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:create"))],
    file: UploadFile = File(...),
    duplicate_action: Annotated[str, Query()] = "skip",
) -> dict:
    """Excel 파일에서 체크리스트 템플릿을 일괄 생성합니다. Owner + GM만 가능.

    Import checklist templates from an Excel file. Owner + GM only.
    """
    if not file.filename or not file.filename.endswith(".xlsx"):
        raise BadRequestError("Only .xlsx files are supported")

    content: bytes = await file.read()
    try:
        result = await checklist_service.import_from_excel(
            db,
            organization_id=current_user.organization_id,
            file_content=content,
            duplicate_action=duplicate_action,
        )
    except ValueError as e:
        raise BadRequestError(str(e))
    await db.commit()
    return result


@router.get(
    "/checklist-templates/import/sample",
)
async def download_sample_excel(
    current_user: Annotated[User, Depends(require_permission("checklists:read"))],
) -> StreamingResponse:
    """샘플 Excel 템플릿을 다운로드합니다.

    Download a sample Excel template for checklist import.
    """
    excel_bytes: bytes = checklist_service.generate_sample_excel()
    return StreamingResponse(
        BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=checklist_template_sample.xlsx"},
    )


@router.get(
    "/checklist-templates/{template_id}",
    response_model=ChecklistTemplateResponse,
)
async def get_template(
    template_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:read"))],
) -> dict:
    """체크리스트 템플릿 상세를 조회합니다.

    Get checklist template detail.
    """
    template = await checklist_service.get_template_detail(
        db,
        template_id=template_id,
        organization_id=current_user.organization_id,
    )

    return {
        "id": str(template.id),
        "store_id": str(template.store_id),
        "shift_id": str(template.shift_id),
        "position_id": str(template.position_id),
        "title": template.title,
        "item_count": len(template.items) if template.items else 0,
    }


@router.post(
    "/stores/{store_id}/checklist-templates",
    response_model=ChecklistTemplateResponse,
    status_code=201,
)
async def create_template(
    store_id: UUID,
    data: ChecklistTemplateCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:create"))],
) -> dict:
    """새 체크리스트 템플릿을 생성합니다. Owner + GM (담당 매장).

    Create a new checklist template. Owner + GM (assigned stores only).
    """
    await check_store_access(db, current_user, store_id)

    template = await checklist_service.create_template(
        db,
        store_id=store_id,
        organization_id=current_user.organization_id,
        data=data,
    )
    await db.commit()

    return {
        "id": str(template.id),
        "store_id": str(template.store_id),
        "shift_id": str(template.shift_id),
        "position_id": str(template.position_id),
        "title": template.title,
        "item_count": 0,
    }


@router.put(
    "/checklist-templates/{template_id}",
    response_model=ChecklistTemplateResponse,
)
async def update_template(
    template_id: UUID,
    data: ChecklistTemplateUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:update"))],
) -> dict:
    """체크리스트 템플릿을 업데이트합니다. Owner + GM.

    Update a checklist template. Owner + GM only.
    """
    template = await checklist_service.update_template(
        db,
        template_id=template_id,
        organization_id=current_user.organization_id,
        data=data,
    )
    await db.commit()

    # 업데이트 후 항목 포함 상세 조회 — Re-fetch with items for accurate item_count
    refreshed = await checklist_service.get_template_detail(
        db,
        template_id=template_id,
        organization_id=current_user.organization_id,
    )

    return {
        "id": str(refreshed.id),
        "store_id": str(refreshed.store_id),
        "shift_id": str(refreshed.shift_id),
        "position_id": str(refreshed.position_id),
        "shift_name": refreshed.shift.name if refreshed.shift else "",
        "position_name": refreshed.position.name if refreshed.position else "",
        "title": refreshed.title,
        "item_count": len(refreshed.items) if refreshed.items else 0,
    }


@router.delete(
    "/checklist-templates/{template_id}",
    response_model=MessageResponse,
)
async def delete_template(
    template_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:delete"))],
) -> dict:
    """체크리스트 템플릿을 삭제합니다. Owner + GM.

    Delete a checklist template. Owner + GM only.
    """
    await checklist_service.delete_template(
        db,
        template_id=template_id,
        organization_id=current_user.organization_id,
    )
    await db.commit()

    return {"message": "체크리스트 템플릿이 삭제되었습니다 (Checklist template deleted)"}


# === 항목 엔드포인트 (Item Endpoints) ===


@router.get(
    "/checklist-templates/{template_id}/items",
    response_model=list[ChecklistItemResponse],
)
async def list_items(
    template_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:read"))],
) -> list[dict]:
    """템플릿의 체크리스트 항목 목록을 조회합니다.

    List items for a checklist template.
    """
    items = await checklist_service.list_items(
        db,
        template_id=template_id,
        organization_id=current_user.organization_id,
    )

    return [
        {
            "id": str(item.id),
            "title": item.title,
            "description": item.description,
            "verification_type": item.verification_type,
            "recurrence_type": item.recurrence_type,
            "recurrence_days": item.recurrence_days,
            "sort_order": item.sort_order,
        }
        for item in items
    ]


@router.post(
    "/checklist-templates/{template_id}/items",
    response_model=ChecklistItemResponse,
    status_code=201,
)
async def create_item(
    template_id: UUID,
    data: ChecklistItemCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:create"))],
) -> dict:
    """템플릿에 새 항목을 추가합니다. Owner + GM.

    Add a new item to a checklist template. Owner + GM only.
    """
    item = await checklist_service.add_item(
        db,
        template_id=template_id,
        organization_id=current_user.organization_id,
        data=data,
    )
    await db.commit()

    return {
        "id": str(item.id),
        "title": item.title,
        "description": item.description,
        "verification_type": item.verification_type,
        "recurrence_type": item.recurrence_type,
        "recurrence_days": item.recurrence_days,
        "sort_order": item.sort_order,
    }


@router.post(
    "/checklist-templates/{template_id}/items/bulk",
    response_model=list[ChecklistItemResponse],
    status_code=201,
)
async def create_items_bulk(
    template_id: UUID,
    data: ChecklistBulkItemCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:create"))],
) -> list[dict]:
    """템플릿에 여러 항목을 일괄 추가합니다. Owner + GM.

    Bulk-add multiple items to a checklist template. Owner + GM only.
    """
    items = await checklist_service.add_items_bulk(
        db,
        template_id=template_id,
        organization_id=current_user.organization_id,
        data=data,
    )
    await db.commit()

    return [
        {
            "id": str(item.id),
            "title": item.title,
            "description": item.description,
            "verification_type": item.verification_type,
            "recurrence_type": item.recurrence_type,
            "recurrence_days": item.recurrence_days,
            "sort_order": item.sort_order,
        }
        for item in items
    ]


@router.put(
    "/checklist-template-items/{item_id}",
    response_model=ChecklistItemResponse,
)
async def update_item(
    item_id: UUID,
    data: ChecklistItemUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:update"))],
) -> dict:
    """체크리스트 항목을 업데이트합니다. Owner + GM.

    Update a checklist template item. Owner + GM only.
    """
    item = await checklist_service.update_item(
        db,
        item_id=item_id,
        organization_id=current_user.organization_id,
        data=data,
    )
    await db.commit()

    return {
        "id": str(item.id),
        "title": item.title,
        "description": item.description,
        "verification_type": item.verification_type,
        "recurrence_type": item.recurrence_type,
        "recurrence_days": item.recurrence_days,
        "sort_order": item.sort_order,
    }


@router.patch(
    "/checklist-template-items/{item_id}/sort",
    response_model=MessageResponse,
)
async def reorder_items(
    item_id: UUID,
    data: ReorderRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:update"))],
) -> dict:
    """체크리스트 항목의 정렬 순서를 재배치합니다. Owner + GM.

    Reorder checklist template items. Owner + GM only.
    """
    await checklist_service.reorder_items_by_item_id(
        db,
        item_id=item_id,
        organization_id=current_user.organization_id,
        item_ids=data.item_ids,
    )
    await db.commit()

    return {"message": "항목 순서가 변경되었습니다 (Item order updated)"}


@router.delete(
    "/checklist-template-items/{item_id}",
    response_model=MessageResponse,
)
async def delete_item(
    item_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("checklists:delete"))],
) -> dict:
    """체크리스트 항목을 삭제합니다. Owner + GM.

    Delete a checklist template item. Owner + GM only.
    """
    await checklist_service.delete_item(
        db,
        item_id=item_id,
        organization_id=current_user.organization_id,
    )
    await db.commit()

    return {"message": "체크리스트 항목이 삭제되었습니다 (Checklist item deleted)"}
