"""Unit tests — empid_cursor_service (트랙 S3 의 API 층 어댑터).

계약 SoT: `docs/99_inbox/2026-08-18 empid 채번 API계약·규칙.md` §3-2/§3-3/§4.

[작성됨]
- 사유 검증(_clean_reason): 누락/공백 → ERR_REASON_REQUIRED, 500자 절단
- 커서 값 검증: 0·음수·bool·비정수 → ERR_CURSOR_INVALID (DB 접근 전에 걸린다)
- initial_cursor: 매장값 > 그룹값 > 1 폴백 (org_numbering 의 floor 규칙과 동일)
- 스코프 상수가 채번 게이트웨이 상수와 같은 문자열인지 (계약 §3-1 의 scope 값)
- 에러 코드 3종의 status/문구가 계약 §4 표 그대로인지

DB 를 타는 경로(조회·조정·재계산·소프트 삭제)는
`tests/integration/api/console/test_store_numbering.py` 가 엔드포인트로 덮는다.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.error_codes.empid import (
    ERR_CURSOR_INVALID,
    ERR_RANGE_IGNORED,
    ERR_REASON_REQUIRED,
)
from app.services import empid_cursor_service as svc
from app.services import org_numbering
from app.utils.exceptions import AppError


# ── 사유 검증 ────────────────────────────────────────────────


@pytest.mark.parametrize("reason", [None, "", "   ", "\n\t "])
def test_clean_reason_rejects_missing(reason: str | None) -> None:
    """사유는 필수 — 공백만 있는 것도 누락으로 본다."""
    with pytest.raises(AppError) as exc:
        svc._clean_reason(reason)
    assert exc.value.detail["code"] == "ERR_REASON_REQUIRED"
    assert exc.value.status_code == 422


def test_clean_reason_trims_and_truncates() -> None:
    """앞뒤 공백은 다듬고, 컬럼 길이(500)를 넘으면 자른다."""
    assert svc._clean_reason("  커서 정정  ") == "커서 정정"
    assert len(svc._clean_reason("x" * 900)) == svc.REASON_MAX_LENGTH


# ── 커서 값 검증 ─────────────────────────────────────────────


@pytest.mark.parametrize("value", [0, -1, -7044, True, False])
async def test_set_cursor_rejects_invalid_value(value) -> None:
    """1 미만·bool 은 ERR_CURSOR_INVALID. DB 를 건드리기 전에 걸려야 한다.

    bool 은 int 의 서브클래스라 True 가 1 로 통과할 수 있어 따로 막는다.
    """
    with pytest.raises(AppError) as exc:
        await svc.set_cursor(
            None,  # DB 접근 전에 raise 되므로 세션이 필요 없다
            scope=svc.SCOPE_GROUP,
            scope_id=uuid4(),
            next_empid=value,
            reason="reason",
            actor_id=None,
        )
    assert exc.value.detail["code"] == "ERR_CURSOR_INVALID"
    assert exc.value.status_code == 422


async def test_set_cursor_checks_value_before_reason() -> None:
    """값과 사유가 둘 다 틀리면 값 오류를 먼저 알린다 (입력 순서와 같다)."""
    with pytest.raises(AppError) as exc:
        await svc.set_cursor(
            None,
            scope=svc.SCOPE_STORE,
            scope_id=uuid4(),
            next_empid=0,
            reason=None,
            actor_id=None,
        )
    assert exc.value.detail["code"] == "ERR_CURSOR_INVALID"


async def test_recalculate_requires_reason_only_when_applying() -> None:
    """apply=false 미리보기는 사유 없이 통과해야 한다 (검증은 apply=true 에서만)."""
    with pytest.raises(AppError) as exc:
        await svc.recalculate_cursor(
            None,
            scope=svc.SCOPE_GROUP,
            scope_id=uuid4(),
            apply=True,
            reason=None,
            actor_id=None,
        )
    assert exc.value.detail["code"] == "ERR_REASON_REQUIRED"


# ── 커서 초기화 ──────────────────────────────────────────────


def test_initial_cursor_falls_back_store_group_one() -> None:
    """매장값 > 그룹값 > 1 — org_numbering 의 floor 폴백과 같은 순서."""
    assert svc.initial_cursor(7000, 1000) == 7000
    assert svc.initial_cursor(None, 1000) == 1000
    assert svc.initial_cursor(None, None) == 1
    assert svc.initial_cursor(None) == 1


# ── 계약 상수/코드 ───────────────────────────────────────────


def test_scope_constants_match_gateway() -> None:
    """scope 문자열은 채번 게이트웨이와 같은 값이어야 한다 (콘솔이 이걸로 분기한다)."""
    assert svc.SCOPE_GROUP == org_numbering.EMPID_SCOPE_GROUP == "group"
    assert svc.SCOPE_STORE == org_numbering.EMPID_SCOPE_STORE == "store"


def test_error_codes_match_contract_table() -> None:
    """계약 §4 표 — status 와 문구(원인 + 다음 행동)를 그대로 쓴다."""
    assert (ERR_REASON_REQUIRED.status_code, ERR_REASON_REQUIRED.message) == (
        422,
        "Enter a reason for this change.",
    )
    assert (ERR_CURSOR_INVALID.status_code, ERR_CURSOR_INVALID.message) == (
        422,
        "Next EMPID must be a whole number of 1 or more.",
    )
    assert (ERR_RANGE_IGNORED.status_code, ERR_RANGE_IGNORED.message) == (
        422,
        "This store follows its group's shared numbering. Change it in Groups.",
    )
