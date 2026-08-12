"""에러 봉투 **우회 경로** 래칫 — 늘어나면 빨간불.

왜 이 파일이 있는가
-------------------
`error_envelope` 는 **`raise` 한 에러만** 덮는다. 전역 핸들러를 타야 봉투(code/message/
trace_id)가 붙기 때문이다. 그래서 봉투를 우회하는 방법이 두 가지 남는다.

1. 에러를 `return` 한다 — `return JSONResponse({...}, status_code=400)`.
   핸들러를 아예 안 거치므로 봉투도 trace_id 도 없이 나간다.
2. 200 인데 본문에 실패를 담는다 — `{"success": false, ...}`.
   상태코드가 성공이라 봉투도 클라 에러 처리도 걸리지 않고, 클라는 성공/실패를
   **본문 관례**로 판정하게 된다(관례는 문서에만 있고 강제되지 않는다).

문서·규칙·메모리는 읽고 무시할 수 있다. 그래서 **세어두고, 늘어나면 실패**시킨다.
`test_error_codes.py` 의 G7 래칫(STATUS_RAISE_BASELINE)과 같은 성격이다.

허용 목록 정책
--------------
- 기준치는 **측정값**이다(측정 날짜를 각 상수 옆에 남긴다). **내려가는 방향으로만** 갱신한다.
  숫자를 올려서 통과시키는 것은 래칫을 끄는 것과 같다.
- 총계가 아니라 **파일별 허용 목록**을 쓴다. 총계만 세면 한 곳을 고치고 다른 곳에 새로
  만들어도 통과한다. 목록 밖에서 하나라도 나오면 실패한다.
- 제품 코드는 고치지 않는다. 기존 위반은 근거와 함께 등재하고 그대로 둔다.

오탐 방지
---------
전부 `ast` 파서로 본다 — 주석/문자열 안의 우연한 일치는 파서 단계에서 애초에 노드가
되지 않으므로 잡히지 않는다(정규식이면 `# return JSONResponse(..., 400)` 같은 주석이나
docstring 예시가 그대로 걸린다).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

# app/ 디렉터리. 스캔 대상은 **제품 코드뿐** — tests/, scripts/, alembic/ 은 애초에
# 이 루트 밖이라 자동으로 제외된다(테스트가 자기 자신을 세면 기준치가 무의미해진다).
PRODUCT_ROOT = Path(__file__).resolve().parents[3] / "app"

# 봉투 **구현체 자신**. 여기서는 4xx/5xx 응답을 만드는 것이 곧 정상 동작이다.
ENVELOPE_IMPL_PREFIXES = (
    "app/core/error_envelope.py",
    "app/middleware/",
    "app/main.py",
)

# 실패를 뜻하는 불리언 플래그 이름. **이 이름들에 리터럴 False 가 붙은 경우만** 본다.
# 도메인 필드(`errors` 리스트, 리뷰 결과 `result`, 어떤 리소스의 `is_error` 컬럼 등)와
# 섞이지 않게 일부러 좁혔다 — 시끄러운 래칫은 곧 무시당하므로 조용하고 정확한 게 낫다.
FAILURE_FLAG_NAMES = frozenset({"success", "ok", "succeeded", "is_success", "is_ok"})


@dataclass(frozen=True)
class Hit:
    path: str  # app/... 상대경로
    line: int
    snippet: str


def _product_files() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for path in sorted(PRODUCT_ROOT.rglob("*.py")):
        rel = path.relative_to(PRODUCT_ROOT.parent).as_posix()
        if rel.startswith(ENVELOPE_IMPL_PREFIXES):
            continue
        out.append((rel, path))
    return out


def _callee_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _static_status(node: ast.expr) -> int | None:
    """status_code 표현식을 정적으로 읽는다. 못 읽으면 None.

    읽는 것: 정수 리터럴(`400`)과 `status.HTTP_404_NOT_FOUND` 류 상수 이름.
    후자는 fastapi/starlette 의 표준 상수라 이름에서 숫자가 결정된다.

    **못 읽는 경우(변수·인자·속성 참조)를 여기서 위반으로 판정하지 않는 이유**:
    호출부 값을 따라가려면 파일 간 상수 전파가 필요하고, 그러면 오탐이 생겨 래칫이
    시끄러워진다. 대신 "정적으로 못 읽는 return Response" 자체를 별도 래칫
    (`test_dynamic_status_responses_do_not_grow`)으로 세어 **숨을 곳이 되지 않게** 막는다.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Attribute) and node.attr.startswith("HTTP_"):
        parts = node.attr.split("_")
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1])
    return None


def _status_args(call: ast.Call) -> list[ast.expr]:
    """이 호출이 들고 있는 status_code 후보 표현식들."""
    found = [kw.value for kw in call.keywords if kw.arg == "status_code"]
    # JSONResponse(content, status_code) 처럼 두 번째 위치 인자가 status 인 경우.
    # 위치 규약을 아는 것은 Response 계열뿐이라 이름으로 제한한다.
    if not found and _callee_name(call).endswith("Response") and len(call.args) >= 2:
        found.append(call.args[1])
    return found


def _scan_returned_error_responses() -> tuple[list[Hit], list[Hit]]:
    """(정적으로 4xx/5xx 인 return, status 를 정적으로 못 읽는 return Response)

    `return <호출>(..., status_code=...)` 형태만 본다. `login_html(..., status_code=401)`
    처럼 **헬퍼로 감싼 우회**도 잡히도록 callee 이름을 Response 계열로 제한하지 않았다.
    """
    static: list[Hit] = []
    dynamic: list[Hit] = []
    for rel, path in _product_files():
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - 제품 코드가 깨지면 다른 테스트가 먼저 죽는다
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            args = _status_args(call)
            if not args:
                continue
            snippet = lines[node.lineno - 1].strip()
            resolved = [_static_status(a) for a in args]
            if any(s is not None and s >= 400 for s in resolved):
                static.append(Hit(rel, node.lineno, snippet))
            elif all(s is None for s in resolved) and _callee_name(call).endswith("Response"):
                dynamic.append(Hit(rel, node.lineno, snippet))
    return static, dynamic


def _scan_body_level_failure_flags() -> list[Hit]:
    """본문에 실패를 담는 지점 — 플래그 이름 + **리터럴 False** 조합만 본다.

    세 가지 표기를 모두 본다(같은 뜻이 세 모양으로 쓰이므로):
      1. dict 리터럴          `{"success": False, ...}`
      2. 호출 키워드          `SomeResponse(success=False, ...)`
      3. 스키마 클래스 필드    `class R(BaseModel): success: bool = False`

    값이 변수인 경우(`{"ok": ok}`)는 보지 않는다 — 성공/실패 양쪽에 쓰이는 정상 패턴이라
    잡으면 오탐이 된다. 리터럴 False 는 "실패를 200 으로 내보낸다"는 뜻 외에 해석의 여지가 없다.
    """
    hits: list[Hit] = []
    for rel, path in _product_files():
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover
            continue

        def add(node: ast.AST) -> None:
            line = getattr(node, "lineno", 1)
            hits.append(Hit(rel, line, lines[line - 1].strip()))

        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value in FAILURE_FLAG_NAMES
                        and isinstance(value, ast.Constant)
                        and value.value is False
                    ):
                        add(value)
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if (
                        kw.arg in FAILURE_FLAG_NAMES
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is False
                    ):
                        add(kw.value)
            elif isinstance(node, ast.ClassDef):
                # 응답 스키마의 기본값 False — 인스턴스 만들 때 아무 말 없이 실패 본문이 된다.
                for stmt in node.body:
                    if (
                        isinstance(stmt, ast.AnnAssign)
                        and isinstance(stmt.target, ast.Name)
                        and stmt.target.id in FAILURE_FLAG_NAMES
                        and isinstance(stmt.value, ast.Constant)
                        and stmt.value.value is False
                    ):
                        add(stmt)
    return hits


def _format(hits: list[Hit]) -> str:
    return "\n".join(f"    {h.path}:{h.line}  {h.snippet}" for h in sorted(hits, key=lambda h: (h.path, h.line)))


def _by_file(hits: list[Hit]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for hit in hits:
        counts[hit.path] = counts.get(hit.path, 0) + 1
    return counts


def _check(hits: list[Hit], baseline: dict[str, int], what: str, instead: str) -> None:
    """허용 목록 밖 신규 + 목록 안 증가를 함께 본다."""
    counts = _by_file(hits)
    new_files = {p for p in counts if p not in baseline}
    grown = {p: (n, baseline[p]) for p, n in counts.items() if p in baseline and n > baseline[p]}
    if not new_files and not grown:
        return
    offenders = [h for h in hits if h.path in new_files or h.path in grown]
    detail = ""
    if grown:
        detail = "\n  기존 파일에서 개수가 늘었다: " + ", ".join(
            f"{p} {now}개 (기준 {was})" for p, (now, was) in sorted(grown.items())
        )
    raise AssertionError(
        f"\n{what}\n"
        f"{_format(offenders)}\n"
        f"{detail}\n"
        f"\n  대신 이렇게 할 것: {instead}\n"
        f"  정말 예외라면: 이 파일의 허용 목록 dict 에 "
        f"파일 경로와 개수를 **근거 주석과 함께** 추가할 것. "
        f"기준치는 내려가는 방향으로만 갱신한다(올려서 통과시키지 말 것)."
    )


# ---------------------------------------------------------------------------
# 래칫 1 — 에러를 return 으로 내보내는 것
# ---------------------------------------------------------------------------

# 2026-08-11 측정값(파일별 개수). **오직 내려가야 한다.**
# 둘 다 운영자 전용 백오피스(서버 렌더 HTML/에디터 XHR)로, API 클라(app/console)가
# 소비하지 않아 봉투 계약 밖이다. 그래서 남겨두되 **더 늘지는 못하게** 등재한다.
RETURNED_ERROR_RESPONSE_BASELINE: dict[str, int] = {
    # 에디터 이미지 업로드 XHR — 401/400/400. 봉투 없이 {"error": ...} 로 나간다.
    "app/api/backoffice/tools/changelog.py": 3,
    # 운영자 로그인 페이지를 401/429 로 다시 렌더 — JSON 이 아니라 HTML 응답이다.
    "app/api/backoffice/routes.py": 2,
}

# 2026-08-11 측정값. status_code 를 정적으로 못 읽는 `return *Response(...)` 지점.
# 여기가 늘면 "동적 status" 가 래칫을 피하는 구멍이 된다.
DYNAMIC_STATUS_RESPONSE_BASELINE: dict[str, int] = {
    # 백오피스 HTML 페이지 렌더 헬퍼 `render(title, body, status_code=200)` 한 곳.
    # 실제 4xx 를 넘기는 호출부(routes.py 의 login_html(..., status_code=401/429))는
    # 위 정적 래칫이 이미 잡으므로, 여기서는 "헬퍼가 하나 더 생기는 것"만 막으면 된다.
    "app/api/backoffice/pages.py": 1,
}


def test_returned_error_responses_do_not_grow() -> None:
    static, _ = _scan_returned_error_responses()
    _check(
        static,
        RETURNED_ERROR_RESPONSE_BASELINE,
        "에러를 `return` 으로 내보내는 지점이 허용 목록 밖에서 새로 생겼다. "
        "return 한 응답은 전역 예외 핸들러를 거치지 않으므로 봉투도 trace_id 도 붙지 않는다.",
        "`raise` 로 바꿀 것 — app/core/error_codes 의 코드를 raise 하면 "
        "(예: `raise PIN_CONFLICT(...)`) 봉투가 자동으로 덮는다.",
    )


def test_dynamic_status_responses_do_not_grow() -> None:
    _, dynamic = _scan_returned_error_responses()
    _check(
        dynamic,
        DYNAMIC_STATUS_RESPONSE_BASELINE,
        "status_code 를 정적으로 읽을 수 없는 `return *Response(...)` 가 새로 생겼다. "
        "이 형태는 4xx 를 숨길 수 있어 위 래칫이 못 본다.",
        "에러 응답이면 `raise` 로 바꾸고, 정상 응답이면 status_code 를 리터럴이나 "
        "`status.HTTP_2xx_*` 상수로 직접 적을 것.",
    )


# ---------------------------------------------------------------------------
# 래칫 2 — 200 인데 본문에 실패를 담는 것
# ---------------------------------------------------------------------------

# 2026-08-11 측정값: **0곳**. 하나라도 생기면 실패한다.
# (비어 있는 것이 정상 상태다 — 항목이 늘어난 채로 오래 남으면 그건 트랙이 멈춘 신호다.)
BODY_FAILURE_FLAG_BASELINE: dict[str, int] = {}


def test_no_success_false_in_response_bodies() -> None:
    _check(
        _scan_body_level_failure_flags(),
        BODY_FAILURE_FLAG_BASELINE,
        "성공 상태코드에 실패를 담는 본문 플래그(`success=False` 류)가 생겼다. "
        "클라가 성공/실패를 본문 관례로 판정하게 되고, 봉투·상태코드·trace_id 어느 것도 걸리지 않는다.",
        "실패는 상태코드로 알릴 것 — app/core/error_codes 의 코드를 `raise` 한다.",
    )


def test_baselines_have_no_slack() -> None:
    """기준치가 실측보다 크면 안 된다 — **줄었으면 그만큼 즉시 잠근다.**

    왜 필요한가: 위 세 래칫은 "늘어남"만 본다. 그래서 changelog.py 3곳 중 2곳을 고쳐도
    기준치 3은 그대로 남고, **빈 2칸이 다음 세션의 우회 여유분**이 된다. 고친 만큼
    잠그지 않으면 래칫이 아니라 그냥 상한선이다. console/app 저장소의 같은 래칫에는
    이 검사가 있는데 여기만 없어서 맞춘다.
    """
    static, dynamic = _scan_returned_error_responses()
    stale: list[str] = []
    for baseline, hits, label in (
        (RETURNED_ERROR_RESPONSE_BASELINE, static, "RETURNED_ERROR_RESPONSE_BASELINE"),
        (DYNAMIC_STATUS_RESPONSE_BASELINE, dynamic, "DYNAMIC_STATUS_RESPONSE_BASELINE"),
        (BODY_FAILURE_FLAG_BASELINE, _scan_body_level_failure_flags(), "BODY_FAILURE_FLAG_BASELINE"),
    ):
        counts = _by_file(hits)
        for path, allowed in baseline.items():
            actual = counts.get(path, 0)
            if actual < allowed:
                stale.append(f"{label}[{path!r}]: 실제 {actual}개인데 기준치가 {allowed} — {actual} 로 낮출 것")
    assert not stale, (
        "\n위반이 줄었다. 줄어든 만큼 기준치를 낮춰야 되돌아오지 못한다"
        "(남겨두면 그 차이가 그대로 다음 우회 여유분이 된다).\n" + "\n".join(f"    {s}" for s in stale)
    )


def test_scanner_actually_sees_the_known_violations() -> None:
    """스캐너가 살아 있는지 확인 — 규칙이 조용히 0건을 세기 시작하면 래칫은 껍데기가 된다.

    (예: 경로 계산이 틀려 스캔 대상이 비어도 위 테스트들은 전부 통과한다.)
    """
    static, dynamic = _scan_returned_error_responses()
    assert len(_product_files()) > 100, "스캔 대상이 비었다 — 경로 계산을 확인할 것"
    # 등재된 위반이 그대로 보이는지. (개수는 보지 않는다 — 줄이는 방향의 수정을 막지 않으려고.)
    assert any(h.path == "app/api/backoffice/tools/changelog.py" for h in static)
    assert any(h.path == "app/api/backoffice/pages.py" for h in dynamic)
