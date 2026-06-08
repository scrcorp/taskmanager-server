"""평가(Evaluation) 도메인 공용 상수 + 템플릿 config JSONB 검증.

평가 템플릿 정의(평가 항목/척도)는 `eval_templates.config` JSONB에 저장되고,
평가 기록 작성 시 `evaluations.template_snapshot` JSONB로 deep-copy 스냅샷된다.
이 파일이 그 JSONB의 형태를 정의/검증하고, v1의 고정 "Basic" 9개 항목을
단일 진실의 원천(single source of truth)으로 보관한다.

v1은 코드 상수(BASIC_CRITERIA/BASIC_SCALE)로 빌트인 Basic 템플릿을 시드한다.
빌더 UI는 v2. 같은 config shape를 그대로 재사용하므로 마이그레이션 없이 확장된다.

JSONB shape (eval_templates.config == evaluations.template_snapshot):
    {
        "criteria": [
            {"code": str, "label": str, "description": str,
             "max_score": int, "sort_order": int},
            ...  # 9 entries, sort_order 1..9
        ],
        "scale": [
            {"value": int, "label": str},
            ...  # 5 entries, value 1..5
        ]
    }
"""

from __future__ import annotations

from copy import deepcopy

from pydantic import BaseModel, Field

# ── 고정 9개 평가 항목 (Basic seed — verbatim) ────────────────────────
# 현장 종이 평가지(temp/evolution_sample.pdf)에서 그대로 전사. mockup lib/criteria.ts 와 동일.
# 1번 항목 description 의 대시는 em-dash(U+2014) 그대로 유지.
# 모든 항목 max_score = 5. sort_order 는 1..9.
BASIC_CRITERIA: list[dict] = [
    {
        "code": "communication",
        "label": "Communication of Work",
        "description": "Efficient, precise, in-time communication — not repeated",
        "max_score": 5,
        "sort_order": 1,
    },
    {
        "code": "work_quality",
        "label": "Work Quality",
        "description": "Work performed according to standards & requirements",
        "max_score": 5,
        "sort_order": 2,
    },
    {
        "code": "efficiency",
        "label": "Efficiency of Work",
        "description": "Amount completed in relation to standards",
        "max_score": 5,
        "sort_order": 3,
    },
    {
        "code": "dependability",
        "label": "Dependability",
        "description": "Follow-through; complete work on time; punctual",
        "max_score": 5,
        "sort_order": 4,
    },
    {
        "code": "teamwork",
        "label": "Attitude and Teamwork",
        "description": "Understanding of job functions and responsibilities",
        "max_score": 5,
        "sort_order": 5,
    },
    {
        "code": "reliability",
        "label": "Reliability",
        "description": "Record of attendance & tardiness for work",
        "max_score": 5,
        "sort_order": 6,
    },
    {
        "code": "housekeeping",
        "label": "Housekeeping",
        "description": "Cleanliness, organization & order of work area",
        "max_score": 5,
        "sort_order": 7,
    },
    {
        "code": "personal_care",
        "label": "Personal Care",
        "description": "Grooming, dress, health, personal cleanliness",
        "max_score": 5,
        "sort_order": 8,
    },
    {
        "code": "judgment",
        "label": "Judgment",
        "description": "Ability to respond to varying situations & make sound decisions",
        "max_score": 5,
        "sort_order": 9,
    },
]

# ── 고정 5점 척도 (Basic seed — verbatim) ─────────────────────────────
BASIC_SCALE: list[dict] = [
    {"value": 1, "label": "Poor"},
    {"value": 2, "label": "Fair"},
    {"value": 3, "label": "Satisfactory"},
    {"value": 4, "label": "Good"},
    {"value": 5, "label": "Excellent"},
]

# ── Basic 템플릿 표시명 ────────────────────────────────────────────────
BASIC_TEMPLATE_NAME = "Basic Performance Evaluation"


def build_default_config() -> dict:
    """Basic 템플릿의 config JSONB 를 새로 생성해 반환.

    매번 deepcopy 하므로 호출자가 반환값을 수정해도 모듈 상수가 오염되지 않는다.
    시드(`ensure_basic_template`) 시 `eval_templates.config` 로,
    평가 작성 시 `evaluations.template_snapshot` 의 베이스로 사용된다.
    """
    return {
        "criteria": deepcopy(BASIC_CRITERIA),
        "scale": deepcopy(BASIC_SCALE),
    }


# ── config JSONB 검증 스키마 (Pydantic v2) ────────────────────────────
class CriterionConfig(BaseModel):
    """평가 항목 한 개의 정의 (config.criteria[i])."""

    code: str
    label: str
    description: str
    max_score: int = Field(ge=1)
    sort_order: int = Field(ge=1)


class ScalePoint(BaseModel):
    """평가 척도의 한 점 (config.scale[i])."""

    value: int = Field(ge=1)
    label: str


class EvalTemplateConfig(BaseModel):
    """`eval_templates.config` / `evaluations.template_snapshot` JSONB 의 형태.

    이 모델로 검증/직렬화하면 항상 §2 canonical shape 를 보장한다.
    응답 스키마(schemas/evaluation.py)는 criteria/scale 서브모델을 재사용한다.
    """

    criteria: list[CriterionConfig]
    scale: list[ScalePoint]


def validate_config(config: dict) -> EvalTemplateConfig:
    """raw dict(JSONB) 를 EvalTemplateConfig 로 검증해 반환. 실패 시 ValidationError."""
    return EvalTemplateConfig.model_validate(config)
