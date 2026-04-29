"""공개 가입 라우터 — Application 제출 + 첨부 업로드.

Public Applications Router — `/join/{encoded}` 가입 페이지에서 호출.
인증 없음. encoded store_id로 매장 식별.
"""

from __future__ import annotations

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
            StoreHiringForm.is_current.is_(True),
        )
    )
    form = form_res.scalar_one_or_none()
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


class SubmitBody(BaseModel):
    encoded: str
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
        # 신규
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

    # 6. 폼 + 검증
    form_res = await db.execute(
        select(StoreHiringForm).where(
            StoreHiringForm.store_id == store_id,
            StoreHiringForm.is_current.is_(True),
        )
    )
    form = form_res.scalar_one_or_none()
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
