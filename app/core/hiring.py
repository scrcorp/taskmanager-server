"""Hiring 도메인 공용 상수 + 폼 스키마 검증.

Form 정의(질문/첨부)는 store_hiring_forms.config JSONB에 저장되며,
이 파일이 그 JSONB의 형태를 정의/검증한다. 스키마 변경 시 새 form version으로
스냅샷되므로, 이전 지원자 데이터는 영향 받지 않는다.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

# ── 첨부 파일 ──────────────────────────────────────────────────
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20 MB
MAX_ATTACHMENT_MB = 20

# 카테고리 → 허용 MIME 매핑. 매장은 카테고리 키만 고른다.
ACCEPT_PRESETS: dict[str, list[str]] = {
    "pdf": ["application/pdf"],
    "image": ["image/jpeg", "image/png", "image/webp", "image/heic"],
    "pdf_or_image": [
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/heic",
    ],
}
AcceptPreset = Literal["pdf", "image", "pdf_or_image"]


# ── 한도 ──────────────────────────────────────────────────────
MAX_QUESTIONS_PER_FORM = 20
MAX_ATTACHMENTS_PER_FORM = 10
MAX_OPTIONS_PER_QUESTION = 20


# ── 질문 스키마 (Form 정의 측) ──────────────────────────────
class QuestionTextDef(BaseModel):
    """한 줄 텍스트 입력."""

    type: Literal["text"]
    id: str
    label: str
    required: bool = False
    placeholder: str | None = None
    max_length: int | None = None


class QuestionNumberDef(BaseModel):
    """숫자 입력."""

    type: Literal["number"]
    id: str
    label: str
    required: bool = False
    placeholder: str | None = None
    min: float | None = None
    max: float | None = None


class QuestionSingleChoiceDef(BaseModel):
    """단일 선택. options 중 하나만."""

    type: Literal["single_choice"]
    id: str
    label: str
    required: bool = False
    options: list[str] = Field(min_length=1, max_length=MAX_OPTIONS_PER_QUESTION)


class QuestionMultiChoiceDef(BaseModel):
    """다중 선택. options 중 0개 이상."""

    type: Literal["multi_choice"]
    id: str
    label: str
    required: bool = False
    options: list[str] = Field(min_length=1, max_length=MAX_OPTIONS_PER_QUESTION)
    min_selected: int = 0
    max_selected: int | None = None


QuestionDef = Annotated[
    Union[
        QuestionTextDef,
        QuestionNumberDef,
        QuestionSingleChoiceDef,
        QuestionMultiChoiceDef,
    ],
    Field(discriminator="type"),
]


# ── 첨부 슬롯 스키마 ──────────────────────────────────────────
class AttachmentSlotDef(BaseModel):
    """매장이 정의하는 첨부 항목 1개."""

    id: str
    label: str
    accept: AcceptPreset = "pdf_or_image"
    required: bool = False


# ── Form 전체 ─────────────────────────────────────────────────
class HiringFormConfig(BaseModel):
    """store_hiring_forms.config JSONB의 정형."""

    welcome_message: str | None = None
    questions: list[QuestionDef] = Field(default_factory=list, max_length=MAX_QUESTIONS_PER_FORM)
    attachments: list[AttachmentSlotDef] = Field(default_factory=list, max_length=MAX_ATTACHMENTS_PER_FORM)


# ── 답변 스냅샷 (지원서 측) ───────────────────────────────────
# applicants.data JSONB 안의 형태. 폼 정의를 그대로 박아 스냅샷으로 보존.
class AnswerSnapshot(BaseModel):
    question_id: str
    label: str  # 제출 시점의 질문 라벨 (폼 변경되어도 유지)
    type: str  # text | number | single_choice | multi_choice
    value: Union[str, float, list[str], None] = None


class AttachmentSnapshot(BaseModel):
    slot_id: str
    label: str  # 제출 시점의 슬롯 라벨
    file_key: str  # storage key (e.g. applicants/{date}/{uuid}.pdf)
    file_name: str
    file_size: int
    mime_type: str


class ApplicantData(BaseModel):
    """applicants.data JSONB의 정형 — 폼 답변/첨부의 스냅샷.

    full_name/email/phone 같은 기본 식별 정보는 applicants 컬럼에 별도 저장.
    """

    answers: list[AnswerSnapshot] = Field(default_factory=list)
    attachments: list[AttachmentSnapshot] = Field(default_factory=list)


# ── Stage ────────────────────────────────────────────────────
ApplicationStage = Literal[
    "new", "reviewing", "interview", "hired", "rejected", "withdrawn"
]
APPLICATION_STAGES: tuple[ApplicationStage, ...] = (
    "new",
    "reviewing",
    "interview",
    "hired",
    "rejected",
    "withdrawn",
)
# 활성 단계 — 한 candidate가 같은 매장에 동시에 여러 active application 못 가짐.
ACTIVE_STAGES: tuple[ApplicationStage, ...] = ("new", "reviewing", "interview")
