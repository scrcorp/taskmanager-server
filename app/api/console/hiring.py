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
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import (
    check_store_access,
    get_accessible_store_ids,
    get_db,
    require_permission,
)
from app.core.hiring import (
    ACTIVE_STAGES,
    APPLICATION_STAGES,
    DEFAULT_FORM_CONFIG,
    HiringFormConfig,
)
from app.core.permissions import STAFF_PRIORITY
from app.models.hiring import (
    Application,
    ApplicationReview,
    Candidate,
    CandidateBlock,
    StoreHiringForm,
)
from app.models.organization import Store
from app.models.user import Role, User
from app.models.user_store import UserStore
from app.services.attendance_device_service import generate_clockin_pin
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
    """매장 hiring 폼 조회 — published/draft 둘 다 반환 (관리자용).

    매장 생성 시 v0 published row 가 자동 삽입되므로 published 는 항상 존재함.
    매니저가 새 폼 만들고 publish 하면 v1, v2, ... 로 올라가고 그쪽이 current.
    """
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
                "is_default": published.version == 0,
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
def _serialize_application(
    app: Application,
    candidate: Candidate,
    *,
    include_data: bool,
    avg_score: Optional[float] = None,
    review_count: int = 0,
) -> dict:
    out = {
        "id": str(app.id),
        "candidate_id": str(candidate.id),
        "store_id": str(app.store_id),
        "form_id": str(app.form_id) if app.form_id else None,
        "attempt_no": app.attempt_no,
        "stage": app.stage,
        # score 는 이제 review 평균 (없으면 None). 기존 컬럼 app.score 는 legacy fallback.
        "score": (
            int(round(avg_score)) if avg_score is not None else app.score
        ),
        "review_count": review_count,
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
    """매장 지원자 목록. stage 쿼리로 필터링 가능 ('active' = new+screen+interview+review)."""
    await check_store_access(db, current_user, store_id)

    # 'all' (default) 은 pending_form 까지 포함 — 회원가입만 하고 폼 미제출인 사람도 표시.
    # 'active' 는 매니저가 처리할 수 있는 단계만 (pending_form 제외).
    stmt = (
        select(Application, Candidate)
        .join(Candidate, Candidate.id == Application.candidate_id)
        .where(Application.store_id == store_id)
        .order_by(desc(Application.submitted_at))
    )
    if stage == "active":
        stmt = stmt.where(Application.stage.in_(("new", "screen", "interview", "review")))
    elif stage and stage in APPLICATION_STAGES:
        stmt = stmt.where(Application.stage == stage)
    # else: default — 모든 stage 포함 (pending_form 도 보이도록)

    result = await db.execute(stmt)
    rows = result.all()
    app_ids = [app.id for app, _ in rows]

    # 배치로 reviews 평균 계산
    avg_map: dict[UUID, tuple[Optional[float], int]] = {}
    if app_ids:
        agg_res = await db.execute(
            select(
                ApplicationReview.application_id,
                func.avg(ApplicationReview.score).label("avg_score"),
                func.count(ApplicationReview.id).label("cnt"),
            )
            .where(
                ApplicationReview.application_id.in_(app_ids),
                ApplicationReview.score.is_not(None),
            )
            .group_by(ApplicationReview.application_id)
        )
        for app_id, avg_s, cnt in agg_res.all():
            avg_map[app_id] = (float(avg_s) if avg_s is not None else None, int(cnt))

    items = [
        _serialize_application(
            app,
            cand,
            include_data=False,
            avg_score=avg_map.get(app.id, (None, 0))[0],
            review_count=avg_map.get(app.id, (None, 0))[1],
        )
        for app, cand in rows
    ]
    counts: dict[str, int] = {s: 0 for s in APPLICATION_STAGES}
    for app, _cand in rows:
        counts[app.stage] = counts.get(app.stage, 0) + 1
    return {"items": items, "counts": counts}


# ────────────────────────────────────────────────────────────────
# Cross-store aggregate list (Inbox)
# ────────────────────────────────────────────────────────────────
async def _avg_score_map(
    db: AsyncSession, app_ids: list[UUID]
) -> dict[UUID, tuple[Optional[float], int]]:
    """application_id → (평균 score, review 수) 배치 조회."""
    out: dict[UUID, tuple[Optional[float], int]] = {}
    if not app_ids:
        return out
    agg_res = await db.execute(
        select(
            ApplicationReview.application_id,
            func.avg(ApplicationReview.score).label("avg_score"),
            func.count(ApplicationReview.id).label("cnt"),
        )
        .where(
            ApplicationReview.application_id.in_(app_ids),
            ApplicationReview.score.is_not(None),
        )
        .group_by(ApplicationReview.application_id)
    )
    for app_id, avg_s, cnt in agg_res.all():
        out[app_id] = (float(avg_s) if avg_s is not None else None, int(cnt))
    return out


@router.get("/applications")
async def list_applications_all(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:read"))],
    store_id: Optional[UUID] = None,
    stage: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "recent",
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """접근 가능한 모든 매장의 지원자를 가로질러 조회 (Inbox).

    스코프는 권한 기반 자동 한정 — Owner는 조직 전체 매장, GM은 관리(is_manager) 매장만.
    각 항목에 store 정보(id/name/code)를 포함해 어느 매장 지원인지 표시한다.

    Query:
      store_id  특정 매장으로 필터 (접근 가능한 매장이어야 함)
      stage     'active'(new+screen+interview+review) 또는 특정 stage
      q         지원자 이름/이메일/username 부분일치 검색
      sort      'recent'(submitted_at desc, 기본) | 'updated'(updated_at desc)
      page/per_page  페이지네이션 (1-base)

    counts 는 store/q 필터는 반영하되 stage 필터·페이지네이션과 무관한 전체 단계별 집계
    (Inbox 상단 summary strip 용).
    """
    org_id = current_user.organization_id
    accessible = await get_accessible_store_ids(db, current_user)

    # GM/SV 인데 관리 매장이 하나도 없으면 즉시 빈 결과
    if accessible is not None and len(accessible) == 0:
        empty_counts = {s: 0 for s in APPLICATION_STAGES}
        return {"items": [], "total": 0, "page": page, "per_page": per_page, "pages": 0, "counts": empty_counts}

    # 특정 매장 필터 시 접근 권한 검증 (403 → 누수 방지)
    if store_id is not None:
        await check_store_access(db, current_user, store_id)

    def _scoped(stmt):
        stmt = (
            stmt.join(Store, Store.id == Application.store_id)
            .where(Store.organization_id == org_id, Store.deleted_at.is_(None))
        )
        if accessible is not None:
            stmt = stmt.where(Application.store_id.in_(accessible))
        if store_id is not None:
            stmt = stmt.where(Application.store_id == store_id)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    Candidate.full_name.ilike(like),
                    Candidate.email.ilike(like),
                    Candidate.username.ilike(like),
                )
            )
        return stmt

    base = _scoped(
        select(Application, Candidate, Store)
        .join(Candidate, Candidate.id == Application.candidate_id)
    )

    # stage 필터 (counts 에는 적용 안 함)
    filtered = base
    if stage == "active":
        filtered = filtered.where(Application.stage.in_(("new", "screen", "interview", "review")))
    elif stage and stage in APPLICATION_STAGES:
        filtered = filtered.where(Application.stage == stage)

    # 정렬
    order_col = desc(Application.updated_at) if sort == "updated" else desc(Application.submitted_at)
    filtered = filtered.order_by(order_col)

    # total
    total: int = (await db.execute(select(func.count()).select_from(filtered.subquery()))).scalar() or 0

    # 페이지 항목
    page = max(1, page)
    per_page = max(1, min(per_page, 100))
    offset = (page - 1) * per_page
    rows = (await db.execute(filtered.offset(offset).limit(per_page))).all()

    avg_map = await _avg_score_map(db, [app.id for app, _c, _s in rows])
    # 인터뷰 4스텝 진행표시용 — 페이지 내 application 중 희망 슬롯을 제출한 것 (batch)
    from app.models.interview import InterviewSlotPreference
    page_ids = [app.id for app, _c, _s in rows]
    picked_ids: set = set()
    if page_ids:
        pref_res = await db.execute(
            select(InterviewSlotPreference.application_id)
            .where(InterviewSlotPreference.application_id.in_(page_ids))
            .distinct()
        )
        picked_ids = {r[0] for r in pref_res.all()}
    # 인터뷰어 이름 batch (확정된 application 표시용)
    iv_ids = [app.interviewer_id for app, _c, _s in rows if app.interviewer_id]
    iv_name_map: dict = {}
    if iv_ids:
        iv_rows = await db.execute(select(User.id, User.full_name).where(User.id.in_(iv_ids)))
        iv_name_map = {uid: name for uid, name in iv_rows.all()}
    items = []
    for app, cand, store in rows:
        item = _serialize_application(
            app,
            cand,
            include_data=False,
            avg_score=avg_map.get(app.id, (None, 0))[0],
            review_count=avg_map.get(app.id, (None, 0))[1],
        )
        item["store"] = {"id": str(store.id), "name": store.name, "code": store.code}
        # 인터뷰 sub-status: requested(토큰발급) → picked(희망제출) → confirmed(확정)
        item["interview_substatus"] = (
            "confirmed" if app.confirmed_slot_id
            else "picked" if app.id in picked_ids
            else "requested" if app.interview_token
            else "not_requested"
        )
        item["interviewer_name"] = iv_name_map.get(app.interviewer_id) if app.interviewer_id else None
        items.append(item)

    # counts — stage 무관, store/q 필터 반영한 전체 단계별 집계
    counts: dict[str, int] = {s: 0 for s in APPLICATION_STAGES}
    count_rows = await db.execute(
        _scoped(
            select(Application.stage, func.count(Application.id))
            .join(Candidate, Candidate.id == Application.candidate_id)
        ).group_by(Application.stage)
    )
    for st, cnt in count_rows.all():
        counts[st] = int(cnt)

    pages = (total + per_page - 1) // per_page
    from app.utils.timezone import get_org_timezone
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "counts": counts,
        # 확정 인터뷰 시각을 org timezone 으로 표시하기 위함 (브라우저 tz 아님)
        "org_timezone": await get_org_timezone(db, org_id),
    }


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

    # reviews — 평가자별 점수+코멘트. reviewer.role 는 아래에서 priority 를 읽으므로
    # selectinload 로 eager load (async 컨텍스트 밖 lazy load → MissingGreenlet 방지).
    reviews_res = await db.execute(
        select(ApplicationReview, User)
        .join(User, User.id == ApplicationReview.reviewer_id)
        .options(selectinload(User.role))
        .where(ApplicationReview.application_id == app_obj.id)
        .order_by(ApplicationReview.created_at)
    )
    review_rows = reviews_res.all()
    review_scores = [r.score for r, _u in review_rows if r.score is not None]
    avg_score: Optional[float] = (
        sum(review_scores) / len(review_scores) if review_scores else None
    )

    out = _serialize_application(
        app_obj,
        candidate,
        include_data=True,
        avg_score=avg_score,
        review_count=len(review_rows),
    )
    out["form_config"] = form_config
    out["reviews"] = [
        {
            "id": str(r.id),
            "reviewer_id": str(r.reviewer_id),
            "reviewer_username": u.username,
            "reviewer_full_name": u.full_name,
            "reviewer_role_priority": u.role.priority if u.role else None,
            "score": r.score,
            "comment": r.comment,
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat(),
            "is_mine": r.reviewer_id == current_user.id,
        }
        for r, u in review_rows
    ]

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
            entered_interview = body.stage == "interview" and app_obj.stage != "interview"
            app_obj.stage = body.stage
            if entered_interview:
                # interview 진입 → 토큰 발급 + 지원자에게 시간 선택 초대 메일 (best-effort)
                from app.services.interview_email_service import issue_and_send_invite
                await issue_and_send_invite(db, app_obj)
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
    """hire 모달에 미리 보여줄 clockin PIN을 발급한다 (단순 6자리 랜덤)."""
    await check_store_access(db, current_user, store_id)
    pin = generate_clockin_pin()
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

        # 클라가 보낸 PIN 사용 (6자리 숫자). 없으면 자동 발급.
        clockin_pin: Optional[str] = None
        if body.clockin_pin and body.clockin_pin.isdigit() and len(body.clockin_pin) == 6:
            clockin_pin = body.clockin_pin
        if clockin_pin is None:
            clockin_pin = generate_clockin_pin()
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
            preferred_language=candidate.preferred_language,
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

    - application.stage = 'review' (인터뷰까지 거쳤으므로 검수 단계로 복귀)
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
        "after": "review",
        "by_user_id": str(current_user.id),
        "by_username": current_user.username,
        "by_full_name": current_user.full_name,
        "at": _now_iso(),
        "note": "Unhired — staff connection to this store removed",
    })
    app_obj.stage = "review"
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


# ────────────────────────────────────────────────────────────────
# Reviews — 평가자별 점수/코멘트 (Owner/GM 등 누구든 자기 review 추가 가능)
# ────────────────────────────────────────────────────────────────
class ReviewUpsertBody(BaseModel):
    score: Optional[int] = Field(default=None, ge=0, le=100)
    comment: Optional[str] = Field(default=None, max_length=2000)


@router.put("/applications/{application_id}/reviews/me")
async def upsert_my_review(
    application_id: UUID,
    body: ReviewUpsertBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:write"))],
) -> dict:
    """현재 사용자의 review 를 upsert. interview 단계 도달 후에만 score 입력 권장.

    score 가 None 이면 코멘트만 (의견만 남김). score 만 있어도 됨.
    """
    res = await db.execute(select(Application).where(Application.id == application_id))
    app_obj = res.scalar_one_or_none()
    if app_obj is None:
        raise HTTPException(status_code=404, detail={"code": "application_not_found"})
    await check_store_access(db, current_user, app_obj.store_id)

    if app_obj.stage == "pending_form":
        raise HTTPException(
            status_code=400,
            detail={"code": "not_yet_submitted", "message": "Wait for the applicant to submit the form."},
        )

    existing_res = await db.execute(
        select(ApplicationReview).where(
            ApplicationReview.application_id == application_id,
            ApplicationReview.reviewer_id == current_user.id,
        )
    )
    review = existing_res.scalar_one_or_none()
    if review is None:
        review = ApplicationReview(
            application_id=application_id,
            reviewer_id=current_user.id,
            score=body.score,
            comment=body.comment,
        )
        db.add(review)
    else:
        review.score = body.score
        review.comment = body.comment
    await db.commit()
    await db.refresh(review)
    return {
        "id": str(review.id),
        "reviewer_id": str(review.reviewer_id),
        "score": review.score,
        "comment": review.comment,
        "created_at": review.created_at.isoformat(),
        "updated_at": review.updated_at.isoformat(),
    }


@router.delete("/applications/{application_id}/reviews/me")
async def delete_my_review(
    application_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_permission("hiring:write"))],
) -> Response:
    """현재 사용자가 남긴 review 삭제."""
    res = await db.execute(select(Application).where(Application.id == application_id))
    app_obj = res.scalar_one_or_none()
    if app_obj is None:
        raise HTTPException(status_code=404, detail={"code": "application_not_found"})
    await check_store_access(db, current_user, app_obj.store_id)

    rev_res = await db.execute(
        select(ApplicationReview).where(
            ApplicationReview.application_id == application_id,
            ApplicationReview.reviewer_id == current_user.id,
        )
    )
    review = rev_res.scalar_one_or_none()
    if review is not None:
        await db.delete(review)
        await db.commit()
    return Response(status_code=204)
