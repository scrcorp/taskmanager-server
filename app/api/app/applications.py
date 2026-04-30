"""공개 가입 라우터 — Application 제출 + 첨부 업로드.

Public Applications Router — `/join/{encoded}` 가입 페이지에서 호출.
인증 없음. encoded store_id로 매장 식별.
"""

from __future__ import annotations

import uuid as _uuid_mod
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.hiring import (
    ACCEPT_PRESETS,
    ACTIVE_STAGES,
    ApplicantData,
    AttachmentSnapshot,
    HiringFormConfig,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_MB,
)
from app.core.url_encoding import decode_uuid
from app.database import get_db
from app.models.hiring import (
    Application,
    Candidate,
    CandidateBlock,
    StoreHiringForm,
)
from app.models.organization import Organization, Store
from app.services.email_verification_service import email_verification_service
from app.services.storage_service import storage_service
from app.utils.password import hash_password

router = APIRouter(prefix="/applications", tags=["App Applications"])


def _normalize_email(email: str) -> str:
    return email.strip().lower()


# ────────────────────────────────────────────────────────────────
# 기존 candidate 로그인 — 다른 매장 지원 / 가입만 하고 이탈한 케이스 이어가기
# ────────────────────────────────────────────────────────────────
class CandidateLoginBody(BaseModel):
    encoded: str
    username: str
    password: str


@router.post("/login")
async def candidate_login(
    body: CandidateLoginBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """이미 가입한 적 있는 candidate가 로그인해서 폼 작성을 이어가도록.

    응답에 verification_token을 dummy로 발급 (이메일 재인증 면제 — 이미 verified).
    클라는 응답의 account 정보 + token을 그대로 들고 form step → submit으로 진행.
    """
    try:
        store_id = decode_uuid(body.encoded)
    except Exception:
        raise HTTPException(status_code=404, detail={"code": "invalid_link"})

    store_res = await db.execute(
        select(Store).where(Store.id == store_id, Store.deleted_at.is_(None))
    )
    store = store_res.scalar_one_or_none()
    if store is None:
        raise HTTPException(status_code=404, detail={"code": "store_not_found"})
    if not store.accepting_signups:
        raise HTTPException(status_code=404, detail={"code": "signups_paused"})

    cand_res = await db.execute(
        select(Candidate).where(Candidate.username == body.username)
    )
    candidate = cand_res.scalar_one_or_none()
    if candidate is None:
        raise HTTPException(
            status_code=401, detail={"code": "invalid_credentials"}
        )
    from app.utils.password import verify_password as _vp
    if not _vp(body.password, candidate.password_hash):
        raise HTTPException(
            status_code=401, detail={"code": "invalid_credentials"}
        )

    # block 검사
    blk_res = await db.execute(
        select(CandidateBlock).where(
            CandidateBlock.candidate_id == candidate.id,
            CandidateBlock.store_id == store_id,
        )
    )
    if blk_res.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=403,
            detail={"code": "not_eligible", "message": "You are not eligible to apply to this store."},
        )

    # 가장 최근 application 가져옴 — 어느 stage든 status 화면에 보여줄 수 있도록.
    # (pending_form: 이어서 폼 작성 / new/reviewing/interview: 진행 상태 표시 /
    #  hired/rejected/withdrawn: 결과 표시)
    latest_res = await db.execute(
        select(Application)
        .where(
            Application.candidate_id == candidate.id,
            Application.store_id == store_id,
        )
        .order_by(Application.submitted_at.desc())
    )
    pending_app = latest_res.scalars().first()

    # email_verified=True인 candidate은 재인증 면제. dummy row + token 발급.
    from app.models.email_verification import EmailVerificationCode
    token = _uuid_mod.uuid4()
    now = datetime.now(timezone.utc)
    dummy = EmailVerificationCode(
        email=candidate.email_normalized,
        code="000000",
        purpose="registration",
        expires_at=now + timedelta(minutes=10),
        is_used=True,
        verification_token=token,
    )
    db.add(dummy)
    await db.commit()

    return {
        "candidate_id": str(candidate.id),
        "username": candidate.username,
        "email": candidate.email,
        "full_name": candidate.full_name,
        "verification_token": str(token),
        "pending_application": (
            {
                "id": str(pending_app.id),
                "store_id": str(pending_app.store_id),
                "stage": pending_app.stage,
            }
            if pending_app
            else None
        ),
    }


# ────────────────────────────────────────────────────────────────
# Form 조회 (공개) — 가입 페이지가 폼 정의 가져가서 동적 렌더
# ────────────────────────────────────────────────────────────────
@router.get("/form/{encoded}")
async def get_public_form(
    encoded: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """공개 가입 페이지가 폼 정의를 가져온다. 폼 미설정 매장은 빈 config 반환."""
    try:
        store_id = decode_uuid(encoded)
    except Exception:
        raise HTTPException(status_code=404, detail={"code": "invalid_link"})

    store_res = await db.execute(
        select(Store).where(
            Store.id == store_id,
            Store.deleted_at.is_(None),
        )
    )
    store = store_res.scalar_one_or_none()
    if store is None:
        raise HTTPException(status_code=404, detail={"code": "store_not_found"})
    if not store.accepting_signups:
        raise HTTPException(status_code=404, detail={"code": "signups_paused"})

    form_res = await db.execute(
        select(StoreHiringForm).where(
            StoreHiringForm.store_id == store_id,
            StoreHiringForm.status == "published",
            StoreHiringForm.is_current.is_(True),
        )
    )
    form = form_res.scalar_one_or_none()
    # 매장 생성 시 v0 published row 가 자동 삽입되므로 form 은 항상 존재해야 함.
    # (혹시라도 없는 케이스는 빈 config 로 대응 — 서버 무결성 문제이지 fallback 아님.)
    return {
        "store_id": str(store_id),
        "form_id": str(form.id) if form else None,
        "config": form.config if form else {"questions": [], "attachments": []},
    }


# ────────────────────────────────────────────────────────────────
# 첨부 업로드 — 제출 전 단계, 응답으로 file_key 받음
# ────────────────────────────────────────────────────────────────
@router.post("/attachments", status_code=201)
async def upload_attachment(
    encoded: Annotated[str, Form()],
    accept: Annotated[str, Form()],
    file: UploadFile = File(...),
) -> dict:
    """공개 첨부 업로드. encoded(매장)와 accept(슬롯의 카테고리)로 검증.

    제출(submit) 전에 호출되어 file_key를 받고, submit body에 그 key를 포함시킨다.
    매장이 실재하는지만 확인하고 application은 아직 만들지 않음.
    """
    # encoded 유효성만
    try:
        decode_uuid(encoded)
    except Exception:
        raise HTTPException(status_code=404, detail={"code": "invalid_link"})

    if accept not in ACCEPT_PRESETS:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_accept", "expected": list(ACCEPT_PRESETS.keys())},
        )
    allowed = ACCEPT_PRESETS[accept]

    if file.content_type not in allowed:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_file_type",
                "message": "File type not allowed for this slot.",
                "expected": allowed,
                "got": file.content_type,
            },
        )

    data = await file.read()
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "file_too_large",
                "message": f"Max {MAX_ATTACHMENT_MB} MB.",
                "max_mb": MAX_ATTACHMENT_MB,
                "actual_mb": round(len(data) / 1024 / 1024, 2),
            },
        )

    key = storage_service.upload_bytes(
        data,
        filename=file.filename or "attachment",
        folder="applicant_attachments",
        content_type=file.content_type,
    )
    return {
        "file_key": key,
        "file_name": file.filename or "attachment",
        "file_size": len(data),
        "mime_type": file.content_type,
    }


# ────────────────────────────────────────────────────────────────
# Submit — candidate 생성/재사용 + application 생성
# ────────────────────────────────────────────────────────────────
class AnswerInput(BaseModel):
    question_id: str
    value: object  # 검증은 폼 정의 기준으로 서버가 수행


class AttachmentInput(BaseModel):
    slot_id: str
    file_key: str
    file_name: str
    file_size: int
    mime_type: str


class StartBody(BaseModel):
    """Step 1 — 회원가입만. application은 stage='pending_form' 으로 생성."""

    encoded: str
    username: str = Field(min_length=3, max_length=50)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=6, max_length=100)
    full_name: str = Field(min_length=1, max_length=255)
    phone: Optional[str] = None
    verification_token: str


class CompleteBody(BaseModel):
    """Step 2 — pending_form application의 폼 답변/첨부 채우기."""

    application_id: str
    form_id: Optional[str] = None
    answers: list["AnswerInput"] = Field(default_factory=list)
    attachments: list["AttachmentInput"] = Field(default_factory=list)


class SubmitBody(BaseModel):
    encoded: str
    # 클라가 폼 fetch 시 받은 form_id. 없으면 폼 미설정 매장으로 간주.
    # 매장이 publish 후에도 진행 중이던 지원자가 자기가 본 폼 그대로 통과하게 하기 위함.
    form_id: Optional[str] = None
    username: str = Field(min_length=3, max_length=50)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=6, max_length=100)
    full_name: str = Field(min_length=1, max_length=255)
    phone: Optional[str] = None
    verification_token: str
    answers: list[AnswerInput] = Field(default_factory=list)
    attachments: list[AttachmentInput] = Field(default_factory=list)


def _validate_against_form(
    config: dict, answers: list[AnswerInput], attachments: list[AttachmentInput]
) -> tuple[list[dict], list[dict]]:
    """폼 정의 기준으로 답변/첨부 검증. 통과하면 스냅샷 형태(answers/attachments)로 반환."""
    form = HiringFormConfig.model_validate(config)
    q_by_id = {q.id: q for q in form.questions}
    a_by_qid = {a.question_id: a for a in answers}

    answer_snaps: list[dict] = []
    for q in form.questions:
        ans = a_by_qid.get(q.id)
        value = ans.value if ans is not None else None
        # required check
        if q.required and (value is None or value == "" or value == []):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "missing_required_answer",
                    "question_id": q.id,
                    "label": q.label,
                },
            )
        answer_snaps.append({
            "question_id": q.id,
            "label": q.label,
            "type": q.type,
            "value": value,
        })

    s_by_id = {s.id: s for s in form.attachments}
    att_by_sid: dict[str, AttachmentInput] = {}
    for a in attachments:
        if a.slot_id not in s_by_id:
            raise HTTPException(
                status_code=400,
                detail={"code": "unknown_slot", "slot_id": a.slot_id},
            )
        # 같은 slot 중복 입력은 마지막 것만
        att_by_sid[a.slot_id] = a

    attachment_snaps: list[dict] = []
    for slot in form.attachments:
        a = att_by_sid.get(slot.id)
        if slot.required and a is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "missing_required_attachment",
                    "slot_id": slot.id,
                    "label": slot.label,
                },
            )
        if a is not None:
            allowed = ACCEPT_PRESETS[slot.accept]
            if a.mime_type not in allowed:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "invalid_attachment_mime",
                        "slot_id": slot.id,
                        "expected": allowed,
                        "got": a.mime_type,
                    },
                )
            attachment_snaps.append({
                "slot_id": slot.id,
                "label": slot.label,
                "file_key": a.file_key,
                "file_name": a.file_name,
                "file_size": a.file_size,
                "mime_type": a.mime_type,
            })

    return answer_snaps, attachment_snaps


# ────────────────────────────────────────────────────────────────
# 진행형 가입 — Step 1: 회원가입만 (pending_form application 생성)
# ────────────────────────────────────────────────────────────────
@router.post("/start", status_code=201)
async def start_application(
    body: StartBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """회원가입 + application 생성 (stage='pending_form').

    이 시점엔 폼 답변/첨부 안 받음. 다음에 로그인해서 /complete 호출해 마저 작성.
    """
    # 1. store
    try:
        store_id = decode_uuid(body.encoded)
    except Exception:
        raise HTTPException(status_code=404, detail={"code": "invalid_link"})
    store_res = await db.execute(
        select(Store).where(Store.id == store_id, Store.deleted_at.is_(None))
    )
    store = store_res.scalar_one_or_none()
    if store is None:
        raise HTTPException(status_code=404, detail={"code": "store_not_found"})
    if not store.accepting_signups:
        raise HTTPException(status_code=404, detail={"code": "signups_paused"})

    # 2. email verification
    await email_verification_service.validate_verification_token(
        db, body.verification_token, body.email
    )

    email_norm = _normalize_email(body.email)

    # 3. candidate lookup / create
    cand_res = await db.execute(
        select(Candidate).where(
            or_(
                Candidate.username == body.username,
                Candidate.email_normalized == email_norm,
            )
        )
    )
    matches = cand_res.scalars().all()
    candidate: Optional[Candidate] = None
    if len(matches) == 0:
        # 신규 — 그 매장 org users.username과 충돌 체크
        from app.models.user import User as _User
        org_user_clash = await db.execute(
            select(_User).where(
                _User.organization_id == store.organization_id,
                _User.username == body.username,
            )
        )
        if org_user_clash.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "username_taken",
                    "message": "This ID is already in use at this store. Please choose a different one.",
                },
            )
        candidate = Candidate(
            username=body.username,
            email=body.email,
            email_normalized=email_norm,
            password_hash=hash_password(body.password),
            email_verified=True,
            full_name=body.full_name,
            phone=body.phone,
        )
        db.add(candidate)
        await db.flush()
        await db.refresh(candidate)
    elif len(matches) == 1:
        m = matches[0]
        if m.username == body.username and m.email_normalized == email_norm:
            from app.utils.password import verify_password as _vp
            if not _vp(body.password, m.password_hash):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "credential_mismatch",
                        "message": "An account with this username and email already exists. Use the existing password.",
                    },
                )
            candidate = m
        else:
            field = "username" if m.username == body.username else "email"
            raise HTTPException(
                status_code=409,
                detail={"code": f"{field}_taken", "message": f"This {field} is already in use."},
            )
    else:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "credentials_split",
                "message": "Username and email belong to different existing accounts.",
            },
        )

    # 4. block 검사
    blk_res = await db.execute(
        select(CandidateBlock).where(
            CandidateBlock.candidate_id == candidate.id,
            CandidateBlock.store_id == store_id,
        )
    )
    if blk_res.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=403,
            detail={"code": "not_eligible", "message": "You are not eligible to apply to this store."},
        )

    # 5. 활성 application 검사 (pending_form 포함 — 이미 작성 중이면 그것 그대로 사용)
    active_res = await db.execute(
        select(Application).where(
            Application.candidate_id == candidate.id,
            Application.store_id == store_id,
            Application.stage.in_(ACTIVE_STAGES),
        )
    )
    existing_active = active_res.scalar_one_or_none()
    if existing_active is not None:
        # 이미 active가 있으면 그대로 반환 (pending_form이면 이어서 작성, 아니면 차단)
        if existing_active.stage == "pending_form":
            return {
                "application_id": str(existing_active.id),
                "candidate_id": str(candidate.id),
                "stage": existing_active.stage,
                "resumed": True,
            }
        raise HTTPException(
            status_code=409,
            detail={
                "code": "active_application_exists",
                "message": "You already have an active application for this store.",
            },
        )

    # 6. attempt_no
    prev_count_res = await db.execute(
        select(Application).where(
            Application.candidate_id == candidate.id,
            Application.store_id == store_id,
        )
    )
    prev_count = len(prev_count_res.scalars().all())

    # 7. application 생성 — 폼 답변/첨부 비어있는 상태
    application = Application(
        candidate_id=candidate.id,
        store_id=store_id,
        form_id=None,  # complete 시 클라가 보낸 form_id로 채움
        attempt_no=prev_count + 1,
        data={"answers": [], "attachments": []},
        stage="pending_form",
    )
    db.add(application)
    await db.commit()
    await db.refresh(application)

    return {
        "application_id": str(application.id),
        "candidate_id": str(candidate.id),
        "stage": application.stage,
        "resumed": False,
    }


# ────────────────────────────────────────────────────────────────
# 진행형 가입 — Step 2: pending_form application 폼 채우고 제출
# ────────────────────────────────────────────────────────────────
@router.post("/complete")
async def complete_application(
    body: CompleteBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """pending_form application 의 답변/첨부 채우고 stage='new'로 전환."""
    try:
        app_uuid = UUID(body.application_id)
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "invalid_application_id"})

    app_res = await db.execute(
        select(Application).where(Application.id == app_uuid)
    )
    application = app_res.scalar_one_or_none()
    if application is None:
        raise HTTPException(status_code=404, detail={"code": "application_not_found"})
    if application.stage != "pending_form":
        raise HTTPException(
            status_code=400,
            detail={"code": "not_pending_form", "message": "This application is not in form-filling state."},
        )

    # 폼 검증
    form: Optional[StoreHiringForm] = None
    if body.form_id:
        try:
            form_uuid = UUID(body.form_id)
        except ValueError:
            raise HTTPException(status_code=400, detail={"code": "invalid_form_id"})
        f_res = await db.execute(
            select(StoreHiringForm).where(StoreHiringForm.id == form_uuid)
        )
        form = f_res.scalar_one_or_none()
        if form is None:
            raise HTTPException(status_code=404, detail={"code": "form_not_found"})
        if form.store_id != application.store_id:
            raise HTTPException(status_code=400, detail={"code": "form_store_mismatch"})
        if form.status != "published":
            raise HTTPException(status_code=400, detail={"code": "form_not_published"})
    else:
        existing_pub = await db.execute(
            select(StoreHiringForm.id).where(
                StoreHiringForm.store_id == application.store_id,
                StoreHiringForm.status == "published",
                StoreHiringForm.is_current.is_(True),
            )
        )
        if existing_pub.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "form_id_required",
                    "message": "This store has a published form. Refresh and submit again.",
                },
            )

    answer_snaps: list[dict] = []
    attachment_snaps: list[dict] = []
    if form is not None:
        answer_snaps, attachment_snaps = _validate_against_form(
            form.config, body.answers, body.attachments
        )

    application.form_id = form.id if form else None
    application.data = {"answers": answer_snaps, "attachments": attachment_snaps}
    application.stage = "new"
    await db.commit()
    await db.refresh(application)

    return {
        "application_id": str(application.id),
        "stage": application.stage,
    }


# ────────────────────────────────────────────────────────────────
# Candidate self-withdraw — 지원자가 본인 application 자진 철회
# ────────────────────────────────────────────────────────────────
class WithdrawBody(BaseModel):
    username: str
    password: str


@router.post("/{application_id}/withdraw")
async def withdraw_application(
    application_id: str,
    body: WithdrawBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """지원자 본인이 application 자진 철회. username+password 로 본인 검증."""
    try:
        app_uuid = UUID(application_id)
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "invalid_application_id"})

    app_res = await db.execute(
        select(Application).where(Application.id == app_uuid)
    )
    application = app_res.scalar_one_or_none()
    if application is None:
        raise HTTPException(status_code=404, detail={"code": "application_not_found"})

    cand_res = await db.execute(
        select(Candidate).where(Candidate.id == application.candidate_id)
    )
    candidate = cand_res.scalar_one_or_none()
    if candidate is None or candidate.username != body.username:
        raise HTTPException(status_code=403, detail={"code": "forbidden"})

    from app.utils.password import verify_password as _vp
    if not _vp(body.password, candidate.password_hash):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})

    if application.stage in ("hired", "rejected", "withdrawn"):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "cannot_withdraw",
                "message": f"Application already in '{application.stage}' state.",
            },
        )

    application.stage = "withdrawn"
    await db.commit()
    await db.refresh(application)
    return {"application_id": str(application.id), "stage": application.stage}


@router.post("/submit", status_code=201)
async def submit_application(
    body: SubmitBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """공개 가입 제출 — candidate 생성/재사용 + application 생성.

    흐름:
      1. encoded → store. accepting_signups 검사.
      2. email verification token 검증.
      3. candidate lookup by username OR email_normalized.
         - 둘 다 없으면 신규 생성.
         - 한 쪽만 매치되면 conflict 응답 (다른 사람이 이미 점유).
         - 둘 다 같은 candidate에 매치되면 password 검증 후 재사용.
      4. CandidateBlock 검사.
      5. 활성 application 검사 (partial unique).
      6. 폼이 있으면 답변/첨부 검증.
      7. application row 생성 (attempt_no = 이전 시도수+1).
    """
    # 1. store
    try:
        store_id = decode_uuid(body.encoded)
    except Exception:
        raise HTTPException(status_code=404, detail={"code": "invalid_link"})
    store_res = await db.execute(
        select(Store).where(Store.id == store_id, Store.deleted_at.is_(None))
    )
    store = store_res.scalar_one_or_none()
    if store is None:
        raise HTTPException(status_code=404, detail={"code": "store_not_found"})
    if not store.accepting_signups:
        raise HTTPException(status_code=404, detail={"code": "signups_paused"})

    # 2. email verification token
    await email_verification_service.validate_verification_token(
        db, body.verification_token, body.email
    )

    email_norm = _normalize_email(body.email)

    # 3. candidate lookup
    cand_res = await db.execute(
        select(Candidate).where(
            or_(
                Candidate.username == body.username,
                Candidate.email_normalized == email_norm,
            )
        )
    )
    matches = cand_res.scalars().all()
    candidate: Optional[Candidate] = None
    if len(matches) == 0:
        # 신규 — 가입 시점에 그 매장 organization의 users.username과도 충돌 체크.
        # 미리 막아두면 hire 시점에 username override 받을 일이 없음.
        from app.models.user import User as _User
        org_user_clash = await db.execute(
            select(_User).where(
                _User.organization_id == store.organization_id,
                _User.username == body.username,
            )
        )
        if org_user_clash.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "username_taken",
                    "message": "This ID is already in use at this store. Please choose a different one.",
                },
            )
        candidate = Candidate(
            username=body.username,
            email=body.email,
            email_normalized=email_norm,
            password_hash=hash_password(body.password),
            email_verified=True,
            full_name=body.full_name,
            phone=body.phone,
        )
        db.add(candidate)
        await db.flush()
        await db.refresh(candidate)
    elif len(matches) == 1:
        m = matches[0]
        if m.username == body.username and m.email_normalized == email_norm:
            # 같은 사람 재이용 — 비번 검증
            from app.utils.password import verify_password as _vp
            if not _vp(body.password, m.password_hash):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "credential_mismatch",
                        "message": "An account with this username and email already exists. Use the existing password to apply with that account.",
                    },
                )
            candidate = m
        else:
            # username 또는 email 한 쪽만 충돌 — 다른 사람이 이미 점유
            field = "username" if m.username == body.username else "email"
            raise HTTPException(
                status_code=409,
                detail={
                    "code": f"{field}_taken",
                    "message": f"This {field} is already in use.",
                },
            )
    else:
        # username/email이 서로 다른 candidate에 분산 매치
        raise HTTPException(
            status_code=409,
            detail={
                "code": "credentials_split",
                "message": "Username and email belong to different existing accounts.",
            },
        )

    # 4. block 검사
    blk_res = await db.execute(
        select(CandidateBlock).where(
            CandidateBlock.candidate_id == candidate.id,
            CandidateBlock.store_id == store_id,
        )
    )
    if blk_res.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "not_eligible",
                "message": "You are not eligible to apply to this store at this time.",
            },
        )

    # 5. 활성 application 검사
    active_res = await db.execute(
        select(Application).where(
            Application.candidate_id == candidate.id,
            Application.store_id == store_id,
            Application.stage.in_(ACTIVE_STAGES),
        )
    )
    if active_res.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "active_application_exists",
                "message": "You already have an active application for this store.",
            },
        )

    # 6. 폼 + 검증 — 클라가 들고 있던 form_id 기준.
    # 진행 중이던 지원자가 매장 publish 후에도 자기가 본 폼대로 제출 가능하게 함.
    form: Optional[StoreHiringForm] = None
    if body.form_id:
        try:
            form_uuid = UUID(body.form_id)
        except ValueError:
            raise HTTPException(status_code=400, detail={"code": "invalid_form_id"})
        f_res = await db.execute(
            select(StoreHiringForm).where(StoreHiringForm.id == form_uuid)
        )
        form = f_res.scalar_one_or_none()
        if form is None:
            raise HTTPException(status_code=404, detail={"code": "form_not_found"})
        if form.store_id != store_id:
            raise HTTPException(status_code=400, detail={"code": "form_store_mismatch"})
        if form.status != "published":
            # draft form_id로 submit 못 함
            raise HTTPException(status_code=400, detail={"code": "form_not_published"})
    else:
        # 클라가 form_id를 안 보냈으면 폼 미설정 매장으로 간주.
        # 단, 매장에 published 폼이 실제로 있는데 form_id를 빠뜨린 거면 reject (실수 방지).
        existing_pub = await db.execute(
            select(StoreHiringForm.id).where(
                StoreHiringForm.store_id == store_id,
                StoreHiringForm.status == "published",
                StoreHiringForm.is_current.is_(True),
            )
        )
        if existing_pub.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "form_id_required",
                    "message": "This store has a published form. Refresh and submit again.",
                },
            )

    answer_snaps: list[dict] = []
    attachment_snaps: list[dict] = []
    if form is not None:
        answer_snaps, attachment_snaps = _validate_against_form(
            form.config, body.answers, body.attachments
        )

    # 7. attempt_no 계산
    prev_count_res = await db.execute(
        select(Application).where(
            Application.candidate_id == candidate.id,
            Application.store_id == store_id,
        )
    )
    prev_count = len(prev_count_res.scalars().all())

    application = Application(
        candidate_id=candidate.id,
        store_id=store_id,
        form_id=form.id if form else None,
        attempt_no=prev_count + 1,
        data={"answers": answer_snaps, "attachments": attachment_snaps},
        stage="new",
    )
    db.add(application)
    await db.commit()
    await db.refresh(application)

    return {
        "application_id": str(application.id),
        "candidate_id": str(candidate.id),
        "stage": application.stage,
        "attempt_no": application.attempt_no,
    }
