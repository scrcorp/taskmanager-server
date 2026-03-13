"""직원 스케줄 신청 템플릿 라우터."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.schedule import RequestTemplateCreate, RequestTemplateResponse, RequestTemplateUpdate
from app.services.schedule_request_service import schedule_request_service

router: APIRouter = APIRouter()


@router.get("/schedule-templates", response_model=list[RequestTemplateResponse])
async def list_templates(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    store_id: str | None = None,
) -> list[RequestTemplateResponse]:
    """내 신청 템플릿 목록. store_id 없으면 전체 반환."""
    if store_id is None:
        return await schedule_request_service.list_all_templates(db, current_user.id)
    return await schedule_request_service.list_templates(db, current_user.id, UUID(store_id))


@router.post("/schedule-templates", response_model=RequestTemplateResponse, status_code=201)
async def create_template(
    data: RequestTemplateCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> RequestTemplateResponse:
    """신청 템플릿 생성."""
    result = await schedule_request_service.create_template(db, current_user.id, data)
    await db.commit()
    return result


@router.put("/schedule-templates/{template_id}", response_model=RequestTemplateResponse)
async def update_template(
    template_id: UUID,
    data: RequestTemplateUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> RequestTemplateResponse:
    """신청 템플릿 수정."""
    result = await schedule_request_service.update_template(db, template_id, current_user.id, data)
    await db.commit()
    return result


@router.delete("/schedule-templates/{template_id}", status_code=204)
async def delete_template(
    template_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """신청 템플릿 삭제."""
    await schedule_request_service.delete_template(db, template_id, current_user.id)
    await db.commit()
