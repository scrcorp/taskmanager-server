"""에러 코드 레지스트리 — 강제 장치(G2·G3·G4·G6·G7)가 실제로 강제되는지.

"선언만 하고 강제 안 하면 안 지켜진다"가 이 트랙의 핵심 교훈이다.
`AppError` 는 2026-06-22 표준으로 도입되고 2개월간 실사용이 1건이었다.
그래서 여기 테스트는 "코드가 있다"가 아니라 **"틀리면 터진다"**를 확인한다.
"""

from __future__ import annotations

from typing import Iterator

import pytest

from app.core import schedule_codes as sc
from app.core import error_codes
from app.core.error_codes import _registry
from app.core.error_codes.attendance import EARLY_CLOCK_IN_REASON_REQUIRED, PIN_CONFLICT
from app.core.error_codes.audit import scan, summary, unregistered_codes
from app.core.error_codes.signup import SIGNUPS_PAUSED
from app.core.error_envelope import error_from_http_exception


@pytest.fixture
def sandbox() -> Iterator[None]:
    """전역 레지스트리를 건드리는 테스트용 — 끝나면 원상복구.

    복구하지 않으면 뒤따르는 덤프 일치 테스트가 테스트용 코드까지 보고 깨진다.
    """
    codes = dict(_registry._BY_CODE)
    domains = dict(_registry._DOMAINS)
    saved = {name: dict(d.codes) for name, d in domains.items()}
    try:
        yield
    finally:
        _registry._BY_CODE.clear()
        _registry._BY_CODE.update(codes)
        _registry._DOMAINS.clear()
        _registry._DOMAINS.update(domains)
        for name, entries in saved.items():
            _registry._DOMAINS[name].codes = entries


# ---------------------------------------------------------------------------
# G2 — 미등록/잘못된 코드는 만들어지지 않는다
# ---------------------------------------------------------------------------


def test_new_codes_must_be_upper_snake(sandbox: None) -> None:
    d = _registry.domain("t_shape")
    with pytest.raises(ValueError, match="UPPER_SNAKE"):
        d.code("lower_snake_code", 400, "nope")


def test_legacy_accepts_existing_lower_snake(sandbox: None) -> None:
    d = _registry.domain("t_legacy")
    entry = d.legacy("some_old_code", 400, "Old code.")
    assert entry.code == "some_old_code"


def test_frozen_requires_client_evidence(sandbox: None) -> None:
    """근거 없는 frozen 은 나중에 아무도 해제하지 못한다 — 그래서 clients 를 강제한다."""
    d = _registry.domain("t_frozen")
    with pytest.raises(ValueError, match="clients"):
        d.legacy("some_frozen_code", 409, "x", frozen=True)


def test_unregistered_schedule_code_still_rejected() -> None:
    """기존 `schedule_codes.issue()` 보호가 그대로 살아 있어야 한다(계약 3-repo 배포됨)."""
    with pytest.raises(ValueError, match="Unregistered schedule code"):
        sc.issue("NOT_A_REAL_CODE")


# ---------------------------------------------------------------------------
# G4 — 전역 중복 검사
# ---------------------------------------------------------------------------


def test_duplicate_code_across_domains_raises(sandbox: None) -> None:
    a = _registry.domain("t_dup_a")
    b = _registry.domain("t_dup_b")
    a.code("SOMETHING_BROKE", 400, "a")
    with pytest.raises(ValueError, match="Duplicate error code"):
        b.code("SOMETHING_BROKE", 400, "b")


def test_duplicate_message_points_at_common(sandbox: None) -> None:
    """오류 문구가 해결책(common 도메인)을 알려줘야 한다 — 안 그러면 도메인마다 이름을 비튼다."""
    a = _registry.domain("t_dup_c")
    b = _registry.domain("t_dup_d")
    a.code("SHARED_THING", 400, "a")
    with pytest.raises(ValueError) as excinfo:
        b.code("SHARED_THING", 400, "b")
    assert "common" in str(excinfo.value)


def test_registry_is_loaded_and_unique() -> None:
    """부팅 시 전 도메인이 import 된다 — import 자체가 중복 검사다."""
    codes = error_codes.all_codes()
    assert len(codes) > 50
    assert len(codes) == len({c for c in codes})
    assert set(error_codes.all_domains()) >= {
        "access",
        "attendance",
        "common",
        "hiring",
        "interviews",
        "payroll",
        "schedule",
        "signup",
        "store",
    }


def test_registry_is_loaded_by_importing_exceptions() -> None:
    """`app.utils.exceptions` 만 import 해도 레지스트리가 서 있어야 한다.

    부팅 경로(`main` → `error_envelope` → `exceptions`)에 걸어 두지 않으면 중복 검사가
    "누군가 그 모듈을 import 했을 때만" 도는 우연이 된다.
    """
    # 새 인터프리터에서 확인한다 — 현재 프로세스의 sys.modules 를 비우고 다시 import 하면
    # 예외 **클래스 객체가 새로 생겨** 다른 테스트의 isinstance 판정이 조용히 어긋난다.
    import subprocess
    import sys

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.utils.exceptions, sys; "
            "print('app.core.error_codes' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "True"


# ---------------------------------------------------------------------------
# G6 — 접근성 대칭: 한 줄로 raise 되고, detail 모양은 계약 그대로
# ---------------------------------------------------------------------------


def test_raise_is_one_line_and_detail_is_flat() -> None:
    exc = PIN_CONFLICT(reason="exact", other_store=False)
    assert exc.status_code == 409
    # 평탄 구조 — 구버전 클라가 최상위에서 읽는다(X3).
    assert exc.detail == {
        "code": "pin_conflict",
        "message": "This PIN is already in use by another employee.",
        "hint": "Choose a different PIN.",
        "reason": "exact",
        "other_store": False,
    }


def test_message_template_is_filled_from_params() -> None:
    exc = EARLY_CLOCK_IN_REASON_REQUIRED(
        minutes_early=23, schedule_id="s1", scheduled_start="2026-08-11T09:00:00"
    )
    assert exc.detail["message"].startswith("This shift starts in 23 minutes.")
    assert exc.detail["minutes_early"] == 23


def test_message_can_be_overridden_per_site() -> None:
    exc = SIGNUPS_PAUSED(message="Hiring resumes on Monday.")
    assert exc.detail["message"] == "Hiring resumes on Monday."


def test_params_cannot_shadow_envelope_fields() -> None:
    """`warnings=` 를 params 로 넘기면 화이트리스트 키와 뜻이 겹쳐 조용히 덮어써진다."""
    with pytest.raises(ValueError, match="collide"):
        PIN_CONFLICT(warnings=["x"])


def test_item_code_cannot_be_raised() -> None:
    zero = error_codes.get("ZERO_DURATION")
    assert zero is not None and zero.kind == "item"
    with pytest.raises(TypeError, match="validation item"):
        zero()


def test_item_issue_drops_none_params() -> None:
    zero = error_codes.get("ZERO_DURATION")
    assert zero is not None
    assert zero.issue(start_at="a", end_at=None) == {
        "code": "ZERO_DURATION",
        "params": {"start_at": "a"},
    }


def test_raised_code_becomes_code_source_domain() -> None:
    """레지스트리로 raise 하면 봉투가 자동으로 domain 코드로 취급한다 — 핸들러 수정 불필요."""
    error = error_from_http_exception(PIN_CONFLICT(reason="exact", other_store=None))
    assert error["code"] == "pin_conflict"
    assert error["code_source"] == "domain"
    assert error["message"] == "This PIN is already in use by another employee."
    assert error["hint"] == "Choose a different PIN."
    assert error["params"] == {"reason": "exact", "other_store": None}


# ---------------------------------------------------------------------------
# E1-d — fallback 문구
# ---------------------------------------------------------------------------


def test_fallback_message_for_registered_code() -> None:
    assert (
        error_codes.fallback_message("signups_paused")
        == "This store is not accepting new sign-ups right now."
    )


def test_fallback_message_for_unknown_code_is_the_code_itself() -> None:
    """일반 문구로 덮으면 "잘못된 안심"이 되어 아무도 그 지점을 고치지 않는다(E1-d)."""
    assert error_codes.fallback_message("no_such_code_here") == "no_such_code_here"


def test_fallback_message_fills_template() -> None:
    assert "23 minutes" in error_codes.fallback_message(
        "early_clock_in_reason_required", minutes_early=23
    )


def test_fallback_message_survives_missing_params() -> None:
    """문구를 만들다 실패해서 표시 자체가 사라지면 안 된다."""
    text = error_codes.fallback_message("early_clock_in_reason_required")
    assert "{minutes_early}" in text


# ---------------------------------------------------------------------------
# 스케줄 흡수 — 기존 계약을 깨지 않는다
# ---------------------------------------------------------------------------


def test_every_schedule_code_is_registered() -> None:
    """`schedule_codes.py` 가 단일 출처 — 거기 추가하면 여기 자동으로 잡혀야 한다."""
    expected = (
        sc.ERROR_CODES
        | sc.WARNING_CODES
        | {sc.SCHEDULE_INVALID, sc.SCHEDULE_WARNINGS_UNCONFIRMED}
    )
    registered = set(error_codes.all_domains()["schedule"].codes)
    assert registered == expected


def test_schedule_messages_come_from_schedule_codes() -> None:
    """문구를 복사해 두 벌로 만들면 갈라진다 — 원본에서 읽는지 확인."""
    entry = error_codes.get("BREAK_REVERSED")
    assert entry is not None
    assert entry.message == sc.fallback_message([{"code": "BREAK_REVERSED", "params": {}}])


@pytest.mark.parametrize(
    "code",
    [
        "pin_conflict",
        "early_clock_in_reason_required",
        "store_closed",
        "NOT_A_MEMBER",
        "ORG_LICENSE_INACTIVE",
        "ORG_ACCESS_REVOKED",
        "provisional_candidate_exists",
        "invalid_link",
        "SCHEDULE_WARNINGS_UNCONFIRMED",
    ],
)
def test_deployed_codes_are_frozen_with_evidence(code: str) -> None:
    """X2 — 이 코드들을 개명하면 배포된 클라가 죽는다. frozen 해제는 clients 확인이 선행."""
    entry = error_codes.get(code)
    assert entry is not None, f"{code} must stay declared"
    assert entry.frozen is True
    assert entry.clients, f"{code}: frozen needs client evidence"


# ---------------------------------------------------------------------------
# G3 — 3-repo 대조 산출물
# ---------------------------------------------------------------------------


def test_registry_dump_is_up_to_date() -> None:
    """console/app 이 읽는 덤프가 소스와 어긋나면 대조 테스트가 거짓 통과한다.

    실패하면: `python -m app.core.error_codes.audit --export`
    """
    assert error_codes.REGISTRY_JSON_PATH.exists()
    on_disk = error_codes.REGISTRY_JSON_PATH.read_text(encoding="utf-8")
    assert on_disk == error_codes.registry_json(), (
        "registry.generated.json is stale — run "
        "`python -m app.core.error_codes.audit --export`"
    )


def test_dump_shape_is_the_client_contract() -> None:
    """클라 테스트가 읽는 필드 — 이름을 바꾸면 3-repo 대조가 조용히 멈춘다."""
    dump = error_codes.export_registry()
    assert dump["version"] == error_codes.REGISTRY_VERSION
    assert set(dump) == {"version", "generated_by", "domains", "codes"}
    sample = dump["codes"][0]
    assert set(sample) == {
        "code",
        "domain",
        "kind",
        "status",
        "message",
        "hint",
        "frozen",
        "clients",
    }
    # 도메인이 늘면 덤프에 자동으로 실린다(스케줄 전용 대조의 일반화).
    assert set(dump["domains"]) == set(error_codes.all_domains())


# ---------------------------------------------------------------------------
# G7 — 진척 지표
# ---------------------------------------------------------------------------

# 2026-08-11 측정값. **오직 내려가야 한다.**
# 올라간다는 것은 새 코드가 또 문자열 raise 로 들어왔다는 뜻이다.
STATUS_RAISE_BASELINE = 674


def test_status_raise_count_does_not_grow() -> None:
    stat = summary(scan())
    assert stat["status"] <= STATUS_RAISE_BASELINE, (
        f"code_source=status raise 가 {stat['status']} 개로 늘었다 "
        f"(기준 {STATUS_RAISE_BASELINE}). 새 에러는 app/core/error_codes 의 코드로 raise 할 것."
    )
    assert stat["total"] == stat["status"] + stat["domain"]


def test_every_raised_code_is_declared() -> None:
    """호출부 리터럴 코드가 레지스트리 밖에 있으면 중복 검사가 그 코드를 못 본다(G4 구멍)."""
    missing = unregistered_codes(scan())
    assert not missing, f"레지스트리에 없는 코드: {sorted(missing)}"
