"""Console report types API (daily 'period' 종류 구성).

report_types 는 org-default(store_id NULL) + store override 행으로 구성된다.
resolution 규칙은 report_service.resolve_effective_types 참조.

GET    /report-types/            — list (raw scope 또는 effective resolved)
POST   /report-types/            — create (org or store scope)
PUT    /report-types/{id}        — update (label/active/deadline/sort)
DELETE /report-types/{id}        — soft delete
POST   /report-types/reorder     — sort_order 일괄 변경
"""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, require_permission
from app.database import get_db
from app.models.user import User
from app.schemas.report import (
    ReportTypeCreate,
    ReportTypeReorder,
    ReportTypeUpdate,
)
from app.services.report_service import report_service

router: APIRouter = APIRouter()


@router.get("/")
async def list_report_types(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("reports:read"))],
    store_id: Annotated[str | None, Query()] = None,
    effective: Annotated[bool, Query()] = False,
) -> dict:
    """report_types 목록.

    - effective=False (default): scope 의 raw 관리 목록. store_id 없으면 org-default 행.
    - effective=True: store 에 실제 적용되는 resolved 목록 (org+store 병합).
    """
    parsed_store_id = UUID(store_id) if store_id else None
    if parsed_store_id is not None:
        await check_store_access(db, current_user, parsed_store_id)
    items = await report_service.list_report_types(
        db,
        organization_id=current_user.organization_id,
        store_id=parsed_store_id,
        effective=effective,
    )
    return {"items": items}


@router.post("/", status_code=201)
async def create_report_type(
    data: ReportTypeCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("report_types:manage"))],
) -> dict:
    if data.store_id:
        await check_store_access(db, current_user, UUID(data.store_id))
    rt = await report_service.create_report_type(
        db, organization_id=current_user.organization_id, data=data
    )
    return report_service.build_report_type_response(rt)


@router.put("/{type_id}")
async def update_report_type(
    type_id: UUID,
    data: ReportTypeUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("report_types:manage"))],
) -> dict:
    rt = await report_service.update_report_type(
        db, type_id=type_id, organization_id=current_user.organization_id, data=data
    )
    if rt.store_id:
        await check_store_access(db, current_user, rt.store_id)
    return report_service.build_report_type_response(rt)


@router.delete("/{type_id}", status_code=204)
async def delete_report_type(
    type_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("report_types:manage"))],
) -> None:
    await report_service.delete_report_type(
        db, type_id=type_id, organization_id=current_user.organization_id
    )


@router.post("/reorder", status_code=204)
async def reorder_report_types(
    data: ReportTypeReorder,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("report_types:manage"))],
) -> None:
    items = [(UUID(i.id), i.sort_order) for i in data.items]
    await report_service.reorder_report_types(
        db, organization_id=current_user.organization_id, items=items
    )
