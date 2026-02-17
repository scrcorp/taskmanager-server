"""관리자 체크리스트 라우터 — 체크리스트 템플릿 및 항목 관리 API.

Admin Checklist Router — API endpoints for checklist template and item management.
Provides CRUD operations for templates and their items, including reordering.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_supervisor
from app.database import get_db
from app.models.user import User
from app.schemas.common import (
    ChecklistItemCreate,
    ChecklistItemResponse,
    ChecklistItemUpdate,
    ChecklistTemplateCreate,
    ChecklistTemplateResponse,
    ChecklistTemplateUpdate,
    MessageResponse,
    ReorderRequest,
)
from app.services.checklist_service import checklist_service

router: APIRouter = APIRouter()


# === 템플릿 엔드포인트 (Template Endpoints) ===


@router.get(
    "/brands/{brand_id}/checklist-templates",
    response_model=list[ChecklistTemplateResponse],
)
async def list_templates(
    brand_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
    shift_id: Annotated[str | None, Query()] = None,
    position_id: Annotated[str | None, Query()] = None,
) -> list[dict]:
    """브랜드별 체크리스트 템플릿 목록을 조회합니다.

    List checklist templates for a brand with optional shift/position filters.

    Args:
        brand_id: 브랜드 UUID 문자열 (Brand UUID string)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)
        shift_id: 근무조 UUID 필터, 선택 (Optional shift UUID filter)
        position_id: 포지션 UUID 필터, 선택 (Optional position UUID filter)

    Returns:
        list[dict]: 템플릿 목록 (List of templates)
    """
    shift_uuid: UUID | None = UUID(shift_id) if shift_id else None
    position_uuid: UUID | None = UUID(position_id) if position_id else None

    templates = await checklist_service.list_templates(
        db,
        brand_id=brand_id,
        organization_id=current_user.organization_id,
        shift_id=shift_uuid,
        position_id=position_uuid,
    )

    return [
        {
            "id": str(t.id),
            "brand_id": str(t.brand_id),
            "shift_id": str(t.shift_id),
            "position_id": str(t.position_id),
            "title": t.title,
            "item_count": len(t.items) if t.items else 0,
        }
        for t in templates
    ]


@router.get(
    "/checklist-templates/{template_id}",
    response_model=ChecklistTemplateResponse,
)
async def get_template(
    template_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """체크리스트 템플릿 상세를 조회합니다.

    Get checklist template detail.

    Args:
        template_id: 템플릿 UUID 문자열 (Template UUID string)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 템플릿 상세 (Template detail)
    """
    template = await checklist_service.get_template_detail(
        db,
        template_id=template_id,
        organization_id=current_user.organization_id,
    )

    return {
        "id": str(template.id),
        "brand_id": str(template.brand_id),
        "shift_id": str(template.shift_id),
        "position_id": str(template.position_id),
        "title": template.title,
        "item_count": len(template.items) if template.items else 0,
    }


@router.post(
    "/brands/{brand_id}/checklist-templates",
    response_model=ChecklistTemplateResponse,
    status_code=201,
)
async def create_template(
    brand_id: UUID,
    data: ChecklistTemplateCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """새 체크리스트 템플릿을 생성합니다.

    Create a new checklist template (unique brand+shift+position).

    Args:
        brand_id: 브랜드 UUID 문자열 (Brand UUID string)
        data: 템플릿 생성 데이터 (Template creation data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 생성된 템플릿 (Created template)
    """
    template = await checklist_service.create_template(
        db,
        brand_id=brand_id,
        organization_id=current_user.organization_id,
        data=data,
    )
    await db.commit()

    return {
        "id": str(template.id),
        "brand_id": str(template.brand_id),
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
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """체크리스트 템플릿을 업데이트합니다.

    Update a checklist template.

    Args:
        template_id: 템플릿 UUID 문자열 (Template UUID string)
        data: 업데이트 데이터 (Update data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 업데이트된 템플릿 (Updated template)
    """
    template = await checklist_service.update_template(
        db,
        template_id=template_id,
        organization_id=current_user.organization_id,
        title=data.title,
    )
    await db.commit()

    return {
        "id": str(template.id),
        "brand_id": str(template.brand_id),
        "shift_id": str(template.shift_id),
        "position_id": str(template.position_id),
        "title": template.title,
        "item_count": 0,
    }


@router.delete(
    "/checklist-templates/{template_id}",
    response_model=MessageResponse,
)
async def delete_template(
    template_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """체크리스트 템플릿을 삭제합니다.

    Delete a checklist template.

    Args:
        template_id: 템플릿 UUID 문자열 (Template UUID string)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 삭제 결과 메시지 (Deletion result message)
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
    current_user: Annotated[User, Depends(require_supervisor)],
) -> list[dict]:
    """템플릿의 체크리스트 항목 목록을 조회합니다.

    List items for a checklist template.

    Args:
        template_id: 템플릿 UUID 문자열 (Template UUID string)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        list[dict]: 항목 목록 (List of items)
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
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """템플릿에 새 항목을 추가합니다.

    Add a new item to a checklist template.

    Args:
        template_id: 템플릿 UUID 문자열 (Template UUID string)
        data: 항목 생성 데이터 (Item creation data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 생성된 항목 (Created item)
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
        "sort_order": item.sort_order,
    }


@router.put(
    "/checklist-template-items/{item_id}",
    response_model=ChecklistItemResponse,
)
async def update_item(
    item_id: UUID,
    data: ChecklistItemUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """체크리스트 항목을 업데이트합니다.

    Update a checklist template item.

    Args:
        item_id: 항목 UUID 문자열 (Item UUID string)
        data: 업데이트 데이터 (Update data)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 업데이트된 항목 (Updated item)
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
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """체크리스트 항목의 정렬 순서를 재배치합니다.

    Reorder checklist template items.

    Note:
        item_id in the path refers to the template item used to identify
        the template. The actual reordering uses the item_ids in the body.

    Args:
        item_id: 항목 UUID (기준 항목) (Item UUID as reference)
        data: 정렬 순서 요청 (Reorder request with item_ids)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 정렬 결과 메시지 (Reorder result message)
    """
    # 항목 ID에서 template_id를 추출하여 재배치 — Resolve template from item and reorder
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
    current_user: Annotated[User, Depends(require_supervisor)],
) -> dict:
    """체크리스트 항목을 삭제합니다.

    Delete a checklist template item.

    Args:
        item_id: 항목 UUID 문자열 (Item UUID string)
        db: 비동기 데이터베이스 세션 (Async database session)
        current_user: 인증된 감독자 이상 사용자 (Authenticated supervisor+ user)

    Returns:
        dict: 삭제 결과 메시지 (Deletion result message)
    """
    await checklist_service.delete_item(
        db,
        item_id=item_id,
        organization_id=current_user.organization_id,
    )
    await db.commit()

    return {"message": "체크리스트 항목이 삭제되었습니다 (Checklist item deleted)"}
