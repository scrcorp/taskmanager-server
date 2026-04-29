"""Admin Hiring 라우터 — 폼 빌더, 지원자 관리, hire, block.

Admin Hiring Router — Form builder, applicants list/detail/stage,
hire (applicant→user), block/unblock.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import (
    check_store_access,
    get_db,
    require_permission,
)
from app.core.hiring import (
    ACTIVE_STAGES,
    APPLICATION_STAGES,
    HiringFormConfig,
)
from app.core.permissions import STAFF_PRIORITY
from app.models.hiring import (
    Application,
    Candidate,
    CandidateBlock,
    StoreHiringForm,
)
from app.models.organization import Store
from app.models.user import Role, User
from app.models.user_store import UserStore
from app.services.attendance_device_service import generate_unique_clockin_pin
from app.utils.password import hash_password

router = APIRouter(prefix="/hiring", tags=["Admin Hiring"])


# ────────────────────────────────────────────────────────────────
# Form Builder
# ────────────────────────────────────────────────────────────────
class FormConfigBody(BaseModel):
    config: dict


class FormResponse(BaseModel):
    id: Optional[str] = None
    version: int = 0
    config: dict
    is_current: bool = False
    updated_at: Optional[str] = None


@router.get("/stores/{store_id}/form")
async def get_hiring_form(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:read"))],
) -> FormConfigBody:
    """매장의 활성 hiring 폼 조회. 없으면 빈 config 반환."""
    await check_store_access(db, current_user, store_id)
    result = await db.execute(
        select(StoreHiringForm)
        .where(
            StoreHiringForm.store_id == store_id,
            StoreHiringForm.is_current.is_(True),
        )
        .limit(1)
    )
    form = result.scalar_one_or_none()
    if form is None:
        return FormConfigBody(config={"questions": [], "attachments": []})
    return FormConfigBody(config=form.config)


@router.put("/stores/{store_id}/form")
async def save_hiring_form(
    store_id: UUID,
    body: FormConfigBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:update"))],
) -> FormConfigBody:
    """매장 hiring 폼 저장 — 새 버전 row 생성, 이전 활성 row의 is_current=False."""
    await check_store_access(db, current_user, store_id)

    # config 검증
    try:
        validated = HiringFormConfig.model_validate(body.config)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"code": "invalid_form", "message": str(e)})

    # 이전 활성 폼 deactivate
    prev_result = await db.execute(
        select(StoreHiringForm).where(
            StoreHiringForm.store_id == store_id,
            StoreHiringForm.is_current.is_(True),
        )
    )
    prev = prev_result.scalar_one_or_none()
    if prev is not None:
        prev.is_current = False
    next_version = (prev.version + 1) if prev is not None else 1

    new_form = StoreHiringForm(
        store_id=store_id,
        version=next_version,
        config=validated.model_dump(mode="json"),
        is_current=True,
        created_by_user_id=current_user.id,
    )
    db.add(new_form)
    await db.commit()
    return FormConfigBody(config=new_form.config)


# ────────────────────────────────────────────────────────────────
# Applications list / detail / stage
# ────────────────────────────────────────────────────────────────
def _serialize_application(app: Application, candidate: Candidate, *, include_data: bool) -> dict:
    out = {
        "id": str(app.id),
        "candidate_id": str(candidate.id),
        "store_id": str(app.store_id),
        "form_id": str(app.form_id) if app.form_id else None,
        "attempt_no": app.attempt_no,
        "stage": app.stage,
        "score": app.score,
        "interview_at": app.interview_at.isoformat() if app.interview_at else None,
        "notes": app.notes,
        "submitted_at": app.submitted_at.isoformat(),
        "updated_at": app.updated_at.isoformat(),
        "candidate": {
            "id": str(candidate.id),
            "username": candidate.username,
            "email": candidate.email,
            "full_name": candidate.full_name,
            "phone": candidate.phone,
            "email_verified": candidate.email_verified,
            "promoted_user_id": str(candidate.promoted_user_id) if candidate.promoted_user_id else None,
        },
    }
    if include_data:
        out["data"] = app.data
    return out


@router.get("/stores/{store_id}/applications")
async def list_applications(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:read"))],
    stage: Optional[str] = None,
) -> dict:
    """매장 지원자 목록. stage 쿼리로 필터링 가능 ('active' = new+reviewing+interview)."""
    await check_store_access(db, current_user, store_id)

    stmt = (
        select(Application, Candidate)
        .join(Candidate, Candidate.id == Application.candidate_id)
        .where(Application.store_id == store_id)
        .order_by(desc(Application.submitted_at))
    )
    if stage == "active":
        stmt = stmt.where(Application.stage.in_(ACTIVE_STAGES))
    elif stage and stage in APPLICATION_STAGES:
        stmt = stmt.where(Application.stage == stage)

    result = await db.execute(stmt)
    rows = result.all()
    items = [
        _serialize_application(app, cand, include_data=False) for app, cand in rows
    ]
    counts: dict[str, int] = {s: 0 for s in APPLICATION_STAGES}
    for app, _cand in rows:
        counts[app.stage] = counts.get(app.stage, 0) + 1
    return {"items": items, "counts": counts}


@router.get("/applications/{application_id}")
async def get_application(
    application_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:read"))],
) -> dict:
    """지원자 상세 + 폼 스냅샷 데이터."""
    result = await db.execute(
        select(Application, Candidate)
        .join(Candidate, Candidate.id == Application.candidate_id)
        .where(Application.id == application_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "application_not_found"})
    app_obj, candidate = row
    await check_store_access(db, current_user, app_obj.store_id)

    # 폼 정의도 함께 (스냅샷 반응을 위해 — 답변 라벨/타입은 application.data에 이미 박혀있지만,
    # 첨부 슬롯 같은 추가 메타 보려면 form config 필요)
    form_config = None
    if app_obj.form_id:
        form_res = await db.execute(
            select(StoreHiringForm).where(StoreHiringForm.id == app_obj.form_id)
        )
        form = form_res.scalar_one_or_none()
        if form is not None:
            form_config = form.config

    out = _serialize_application(app_obj, candidate, include_data=True)
    out["form_config"] = form_config

    # 같은 candidate의 이전 application 횟수 (이력 노출용)
    history_res = await db.execute(
        select(Application)
        .where(
            Application.candidate_id == candidate.id,
            Application.store_id == app_obj.store_id,
            Application.id != app_obj.id,
        )
        .order_by(desc(Application.submitted_at))
    )
    history = history_res.scalars().all()
    out["history"] = [
        {
            "id": str(h.id),
            "attempt_no": h.attempt_no,
            "stage": h.stage,
            "submitted_at": h.submitted_at.isoformat(),
        }
        for h in history
    ]

    # 차단 여부
    block_res = await db.execute(
        select(CandidateBlock).where(
            CandidateBlock.candidate_id == candidate.id,
            CandidateBlock.store_id == app_obj.store_id,
        )
    )
    block = block_res.scalar_one_or_none()
    out["is_blocked"] = block is not None
    out["block"] = (
        {
            "reason": block.reason,
            "created_at": block.created_at.isoformat(),
        }
        if block
        else None
    )

    return out


class ApplicationPatchBody(BaseModel):
    stage: Optional[str] = None
    score: Optional[int] = None
    notes: Optional[str] = None
    interview_at: Optional[datetime] = None


@router.patch("/applications/{application_id}")
async def patch_application(
    application_id: UUID,
    body: ApplicationPatchBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:update"))],
) -> dict:
    """stage / score / notes / interview_at 수정. hire는 별도 endpoint."""
    result = await db.execute(
        select(Application).where(Application.id == application_id)
    )
    app_obj = result.scalar_one_or_none()
    if app_obj is None:
        raise HTTPException(status_code=404, detail={"code": "application_not_found"})
    await check_store_access(db, current_user, app_obj.store_id)

    if body.stage is not None:
        if body.stage not in APPLICATION_STAGES:
            raise HTTPException(status_code=400, detail={"code": "invalid_stage"})
        if body.stage == "hired":
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "use_hire_endpoint",
                    "message": "Use POST /applications/{id}/hire to promote.",
                },
            )
        app_obj.stage = body.stage
    if body.score is not None:
        app_obj.score = body.score
    if body.notes is not None:
        app_obj.notes = body.notes
    if body.interview_at is not None:
        app_obj.interview_at = body.interview_at

    await db.commit()
    cand_res = await db.execute(
        select(Candidate).where(Candidate.id == app_obj.candidate_id)
    )
    candidate = cand_res.scalar_one()
    return _serialize_application(app_obj, candidate, include_data=False)


# ────────────────────────────────────────────────────────────────
# Hire — applicant → user
# ────────────────────────────────────────────────────────────────
class HireBody(BaseModel):
    """username 충돌 시 매니저가 다른 username 지정 가능."""

    username_override: Optional[str] = Field(default=None, min_length=3, max_length=50)


@router.post("/applications/{application_id}/hire")
async def hire_application(
    application_id: UUID,
    body: HireBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:hire"))],
) -> dict:
    """지원자를 user로 승격. user 생성 + user_stores 배정 + candidate.promoted_user_id 갱신.

    같은 candidate가 이전에 다른 매장에서 hire됐으면 user 재사용 + user_stores만 추가.
    """
    result = await db.execute(
        select(Application, Candidate)
        .join(Candidate, Candidate.id == Application.candidate_id)
        .where(Application.id == application_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "application_not_found"})
    app_obj, candidate = row
    await check_store_access(db, current_user, app_obj.store_id)

    if app_obj.stage == "hired":
        raise HTTPException(status_code=400, detail={"code": "already_hired"})

    org_id = current_user.organization_id

    # 매장의 staff role 조회
    role_res = await db.execute(
        select(Role).where(
            Role.organization_id == org_id,
            Role.priority == STAFF_PRIORITY,
        )
    )
    staff_role = role_res.scalar_one_or_none()
    if staff_role is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "no_staff_role", "message": "Staff role not configured."},
        )

    user: Optional[User] = None
    # candidate가 이미 user를 가지고 있으면 재사용
    if candidate.promoted_user_id is not None:
        u_res = await db.execute(select(User).where(User.id == candidate.promoted_user_id))
        user = u_res.scalar_one_or_none()

    if user is None:
        # username 결정 — override > candidate.username
        target_username = body.username_override or candidate.username
        # 같은 org 내 username unique 체크
        existing_res = await db.execute(
            select(User).where(
                User.organization_id == org_id,
                User.username == target_username,
            )
        )
        if existing_res.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "username_taken",
                    "message": f"Username '{target_username}' already exists. Provide username_override.",
                },
            )

        clockin_pin = await generate_unique_clockin_pin(db, org_id)
        user = User(
            organization_id=org_id,
            role_id=staff_role.id,
            username=target_username,
            full_name=candidate.full_name,
            email=candidate.email,
            password_hash=candidate.password_hash,
            email_verified=candidate.email_verified,
            clockin_pin=clockin_pin,
        )
        db.add(user)
        await db.flush()
        await db.refresh(user)
        candidate.promoted_user_id = user.id

    # user_stores 배정 (이미 있으면 skip)
    us_res = await db.execute(
        select(UserStore).where(
            UserStore.user_id == user.id,
            UserStore.store_id == app_obj.store_id,
        )
    )
    if us_res.scalar_one_or_none() is None:
        db.add(UserStore(user_id=user.id, store_id=app_obj.store_id))

    # application stage 업데이트
    app_obj.stage = "hired"
    await db.commit()

    return {
        "user_id": str(user.id),
        "username": user.username,
        "application_id": str(app_obj.id),
        "stage": app_obj.stage,
    }


# ────────────────────────────────────────────────────────────────
# Block / unblock candidate (store-level)
# ────────────────────────────────────────────────────────────────
class BlockBody(BaseModel):
    reason: Optional[str] = None


@router.post("/applications/{application_id}/block")
async def block_candidate(
    application_id: UUID,
    body: BlockBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:block"))],
) -> dict:
    """해당 application의 candidate를 그 매장에서 차단 (재지원 불가).

    이미 차단되어 있으면 reason만 update.
    """
    res = await db.execute(select(Application).where(Application.id == application_id))
    app_obj = res.scalar_one_or_none()
    if app_obj is None:
        raise HTTPException(status_code=404, detail={"code": "application_not_found"})
    await check_store_access(db, current_user, app_obj.store_id)

    blk_res = await db.execute(
        select(CandidateBlock).where(
            CandidateBlock.candidate_id == app_obj.candidate_id,
            CandidateBlock.store_id == app_obj.store_id,
        )
    )
    existing = blk_res.scalar_one_or_none()
    if existing is not None:
        existing.reason = body.reason
        await db.commit()
        return {"blocked": True, "reason": existing.reason}

    block = CandidateBlock(
        candidate_id=app_obj.candidate_id,
        store_id=app_obj.store_id,
        reason=body.reason,
        blocked_by_user_id=current_user.id,
    )
    db.add(block)
    await db.commit()
    return {"blocked": True, "reason": block.reason}


@router.delete("/applications/{application_id}/block")
async def unblock_candidate(
    application_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:block"))],
) -> dict:
    res = await db.execute(select(Application).where(Application.id == application_id))
    app_obj = res.scalar_one_or_none()
    if app_obj is None:
        raise HTTPException(status_code=404, detail={"code": "application_not_found"})
    await check_store_access(db, current_user, app_obj.store_id)

    blk_res = await db.execute(
        select(CandidateBlock).where(
            CandidateBlock.candidate_id == app_obj.candidate_id,
            CandidateBlock.store_id == app_obj.store_id,
        )
    )
    block = blk_res.scalar_one_or_none()
    if block is not None:
        await db.delete(block)
        await db.commit()
    return {"blocked": False}
