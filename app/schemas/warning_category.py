"""경고 사유 카테고리 Pydantic 스키마 — Warning category (v1.1).

House style: snake_case JSON. 관리(추가/이름변경/숨김토글/삭제)는 Owner only(라우터 강제).
code 는 label 에서 슬러그로 파생(서비스) — 요청엔 label 만.

Schemas:
    - WarningCategoryCreate: 카테고리 추가 (label)
    - WarningCategoryUpdate: 이름 변경 / 숨김 토글 (partial)
    - WarningCategoryResponse: 카테고리 응답
"""

from pydantic import BaseModel, field_validator

__all__ = [
    "WarningCategoryCreate",
    "WarningCategoryUpdate",
    "WarningCategoryResponse",
]


def _clean_label(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("Label is required")
    if len(v) > 100:
        raise ValueError("Label too long (max 100 characters)")
    return v


class WarningCategoryCreate(BaseModel):
    """카테고리 추가 — POST /. label 만. 같은 코드 존재 시 서비스가 revive."""

    label: str

    @field_validator("label")
    @classmethod
    def _check_label(cls, v: str) -> str:
        return _clean_label(v)


class WarningCategoryUpdate(BaseModel):
    """카테고리 수정 — PATCH /{id}. 이름 변경 / 숨김 토글 (partial)."""

    label: str | None = None
    is_hidden: bool | None = None

    @field_validator("label")
    @classmethod
    def _check_label(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _clean_label(v)


class WarningCategoryResponse(BaseModel):
    """카테고리 응답 — GET /, POST, PATCH."""

    id: str
    code: str
    label: str
    sort_order: int
    is_hidden: bool
    is_system: bool
