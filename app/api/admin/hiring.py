"""Admin Hiring 라우터 — 폼 빌더, 지원자 관리, hire, block.

Admin Hiring Router — Form builder, applicants list/detail/stage,
hire (applicant→user), block/unblock.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
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


async def _get_published(db: AsyncSession, store_id: UUID) -> Optional[StoreHiringForm]:
    res = await db.execute(
        select(StoreHiringForm).where(
            StoreHiringForm.store_id == store_id,
            StoreHiringForm.status == "published",
            StoreHiringForm.is_current.is_(True),
        )
    )
    return res.scalar_one_or_none()


async def _get_draft(db: AsyncSession, store_id: UUID) -> Optional[StoreHiringForm]:
    res = await db.execute(
        select(StoreHiringForm).where(
            StoreHiringForm.store_id == store_id,
            StoreHiringForm.status == "draft",
        )
    )
    return res.scalar_one_or_none()


@router.get("/stores/{store_id}/form")
async def get_hiring_form(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:read"))],
) -> dict:
    """매장 hiring 폼 조회 — published/draft 둘 다 반환 (관리자용)."""
    await check_store_access(db, current_user, store_id)
    published = await _get_published(db, store_id)
    draft = await _get_draft(db, store_id)
    return {
        "published": (
            {
                "id": str(published.id),
                "version": published.version,
                "config": published.config,
                "updated_at": published.updated_at.isoformat(),
            }
            if published
            else None
        ),
        "draft": (
            {
                "id": str(draft.id),
                "config": draft.config,
                "updated_at": draft.updated_at.isoformat(),
            }
            if draft
            else None
        ),
    }


@router.put("/stores/{store_id}/form")
async def save_hiring_form_draft(
    store_id: UUID,
    body: FormConfigBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:update"))],
) -> dict:
    """draft 저장 (upsert). 지원자한테 영향 X — published 폼은 그대로 유지."""
    await check_store_access(db, current_user, store_id)
    try:
        validated = HiringFormConfig.model_validate(body.config)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"code": "invalid_form", "message": str(e)})

    config_json = validated.model_dump(mode="json")
    draft = await _get_draft(db, store_id)
    if draft is None:
        draft = StoreHiringForm(
            store_id=store_id,
            version=None,
            status="draft",
            config=config_json,
            is_current=False,
            created_by_user_id=current_user.id,
        )
        db.add(draft)
    else:
        draft.config = config_json
    await db.commit()
    await db.refresh(draft)
    return {
        "id": str(draft.id),
        "config": draft.config,
        "updated_at": draft.updated_at.isoformat(),
    }


@router.post("/stores/{store_id}/form/publish")
async def publish_hiring_form(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:update"))],
) -> dict:
    """draft를 published로 승격. 이전 published는 is_current=False로 보존.

    이전 폼 row는 영원 보존 — 진행 중이던 지원자(이전 form_id를 들고 있는)는
    그대로 그 폼으로 submit 가능.
    """
    await check_store_access(db, current_user, store_id)
    draft = await _get_draft(db, store_id)
    if draft is None:
        raise HTTPException(
            status_code=400, detail={"code": "no_draft", "message": "No draft to publish."}
        )
    prev = await _get_published(db, store_id)
    if prev is not None:
        prev.is_current = False
    next_version = (prev.version + 1) if (prev and prev.version is not None) else 1
    draft.status = "published"
    draft.version = next_version
    draft.is_current = True
    await db.commit()
    return {
        "id": str(draft.id),
        "version": draft.version,
        "config": draft.config,
        "updated_at": draft.updated_at.isoformat(),
    }


@router.delete("/stores/{store_id}/form/draft")
async def discard_hiring_form_draft(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:update"))],
) -> dict:
    """draft 폐기. published는 영향 없음."""
    await check_store_access(db, current_user, store_id)
    draft = await _get_draft(db, store_id)
    if draft is not None:
        await db.delete(draft)
        await db.commit()
    return {"discarded": True}


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

    # 같은 candidate의 이전 application 시도들
    history_res = await db.execute(
        select(Application)
        .where(
            Application.candidate_id == candidate.id,
            Application.store_id == app_obj.store_id,
            Application.id != app_obj.id,
        )
        .order_by(desc(Application.submitted_at))
    )
    prev_attempts = history_res.scalars().all()
    out["history"] = [
        {
            "id": str(h.id),
            "attempt_no": h.attempt_no,
            "stage": h.stage,
            "submitted_at": h.submitted_at.isoformat(),
        }
        for h in prev_attempts
    ]
    # stage/score/notes 변경 audit log (역순 — 최근 변경부터)
    out["audit_log"] = list(reversed(app_obj.history or []))

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


def _append_history(app_obj: Application, entry: dict) -> None:
    """applications.history에 audit row append (JSONB list 직접 변경)."""
    from sqlalchemy.orm.attributes import flag_modified
    new_history = list(app_obj.history or [])
    new_history.append(entry)
    app_obj.history = new_history
    flag_modified(app_obj, "history")


def _now_iso() -> str:
    from datetime import datetime as _dt, timezone as _tz
    return _dt.now(_tz.utc).isoformat()


@router.patch("/applications/{application_id}")
async def patch_application(
    application_id: UUID,
    body: ApplicationPatchBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:update"))],
) -> dict:
    """stage / score / notes / interview_at 수정. 변경마다 history 기록. hire는 별도 endpoint."""
    result = await db.execute(
        select(Application).where(Application.id == application_id)
    )
    app_obj = result.scalar_one_or_none()
    if app_obj is None:
        raise HTTPException(status_code=404, detail={"code": "application_not_found"})
    await check_store_access(db, current_user, app_obj.store_id)

    actor = {
        "by_user_id": str(current_user.id),
        "by_username": current_user.username,
        "by_full_name": current_user.full_name,
        "at": _now_iso(),
    }

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
        if app_obj.stage != body.stage:
            _append_history(app_obj, {
                "action": "stage",
                "before": app_obj.stage,
                "after": body.stage,
                **actor,
            })
            app_obj.stage = body.stage
    if body.score is not None and body.score != app_obj.score:
        _append_history(app_obj, {
            "action": "score",
            "before": app_obj.score,
            "after": body.score,
            **actor,
        })
        app_obj.score = body.score
    if body.notes is not None and body.notes != app_obj.notes:
        _append_history(app_obj, {
            "action": "notes",
            "before": app_obj.notes,
            "after": body.notes,
            **actor,
        })
        app_obj.notes = body.notes
    if body.interview_at is not None and body.interview_at != app_obj.interview_at:
        _append_history(app_obj, {
            "action": "interview_at",
            "before": app_obj.interview_at.isoformat() if app_obj.interview_at else None,
            "after": body.interview_at.isoformat(),
            **actor,
        })
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
    """username 충돌 시 매니저가 다른 username 지정 가능.

    user_id / clockin_pin은 클라가 모달에 미리 표시한 값을 그대로 쓰고 싶을 때.
    """

    username_override: Optional[str] = Field(default=None, min_length=3, max_length=50)
    user_id: Optional[str] = None
    clockin_pin: Optional[str] = Field(default=None, min_length=6, max_length=6)


@router.get("/stores/{store_id}/preview-pin")
async def preview_clockin_pin(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:hire"))],
) -> dict:
    """hire 모달에 미리 보여줄 clockin PIN을 발급한다.

    이 엔드포인트가 만든 PIN은 reservation 아님 — 이론상 모달 열고 hire 누르기 전에
    다른 사람 hire가 같은 PIN을 잡을 수 있지만, 100만 공간이라 거의 충돌 없음.
    hire 시점에 server가 다시 unique 검증하므로 충돌 시 자동 재발급.
    """
    await check_store_access(db, current_user, store_id)
    pin = await generate_unique_clockin_pin(db, current_user.organization_id)
    return {"clockin_pin": pin}


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

        # 클라가 미리 보여준 PIN 사용. unique 검증 후 충돌이면 자동 재발급.
        clockin_pin: Optional[str] = None
        if body.clockin_pin and body.clockin_pin.isdigit():
            pin_clash = await db.execute(
                select(User.id).where(
                    User.organization_id == org_id,
                    User.clockin_pin == body.clockin_pin,
                )
            )
            if pin_clash.scalar_one_or_none() is None:
                clockin_pin = body.clockin_pin
        if clockin_pin is None:
            clockin_pin = await generate_unique_clockin_pin(db, org_id)
        # 클라가 미리 보여준 uuid를 받았으면 그대로 사용 (모달의 User ID = 실제 user.id 보장)
        user_kwargs: dict = {}
        if body.user_id:
            try:
                user_kwargs["id"] = UUID(body.user_id)
            except ValueError:
                raise HTTPException(status_code=400, detail={"code": "invalid_user_id"})
        user = User(
            organization_id=org_id,
            role_id=staff_role.id,
            username=target_username,
            full_name=candidate.full_name,
            email=candidate.email,
            password_hash=candidate.password_hash,
            email_verified=candidate.email_verified,
            clockin_pin=clockin_pin,
            **user_kwargs,
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

    # application stage 업데이트 + history
    if app_obj.stage != "hired":
        _append_history(app_obj, {
            "action": "stage",
            "before": app_obj.stage,
            "after": "hired",
            "by_user_id": str(current_user.id),
            "by_username": current_user.username,
            "by_full_name": current_user.full_name,
            "at": _now_iso(),
            "note": f"Hired and created user {user.username}",
        })
    app_obj.stage = "hired"
    await db.commit()

    return {
        "user_id": str(user.id),
        "username": user.username,
        "application_id": str(app_obj.id),
        "stage": app_obj.stage,
    }


@router.post("/applications/{application_id}/unhire")
async def unhire_application(
    application_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:hire"))],
) -> dict:
    """잘못 hire한 application을 되돌린다.

    - application.stage = 'reviewing'
    - 해당 매장과의 user_stores 연결 삭제 (그 매장 staff 아님)
    - user 계정 자체는 유지 (다른 매장 hire 가능성 + candidate.promoted_user_id 참조 보존)
    """
    res = await db.execute(
        select(Application).where(Application.id == application_id)
    )
    app_obj = res.scalar_one_or_none()
    if app_obj is None:
        raise HTTPException(status_code=404, detail={"code": "application_not_found"})
    await check_store_access(db, current_user, app_obj.store_id)

    if app_obj.stage != "hired":
        raise HTTPException(
            status_code=400, detail={"code": "not_hired", "message": "Only hired applications can be unhired."}
        )

    # candidate → user_stores 연결 끊기
    cand_res = await db.execute(
        select(Candidate).where(Candidate.id == app_obj.candidate_id)
    )
    candidate = cand_res.scalar_one()
    user_id = candidate.promoted_user_id
    removed_user_store = False
    if user_id is not None:
        us_res = await db.execute(
            select(UserStore).where(
                UserStore.user_id == user_id,
                UserStore.store_id == app_obj.store_id,
            )
        )
        us = us_res.scalar_one_or_none()
        if us is not None:
            await db.delete(us)
            removed_user_store = True

    # stage 되돌림 + audit log
    _append_history(app_obj, {
        "action": "stage",
        "before": "hired",
        "after": "reviewing",
        "by_user_id": str(current_user.id),
        "by_username": current_user.username,
        "by_full_name": current_user.full_name,
        "at": _now_iso(),
        "note": "Unhired — staff connection to this store removed",
    })
    app_obj.stage = "reviewing"
    await db.commit()

    return {
        "application_id": str(app_obj.id),
        "stage": app_obj.stage,
        "user_id": str(user_id) if user_id else None,
        "removed_user_store": removed_user_store,
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
