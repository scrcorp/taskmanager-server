"""진척 지표(G7) + 3-repo 덤프(G3).

왜 숫자가 필요한가
------------------
봉투를 넣어도 `code_source` 가 `"status"` 인 응답이 줄지 않으면 Phase 3(도메인 코드화)은
멈춘 것이다. 그런데 "멈췄다"를 아무도 모르면 트랙은 조용히 끝난다.
그래서 **서버 소스에서 직접 세는** 지표를 둔다 — 운영 로그가 없어도, 트래픽이 없어도 잰다.

세는 방법
---------
`raise` 지점을 AST 로 훑어 두 부류로 나눈다.

- **domain** — `detail={"code": ...}` 이거나 `code=` 를 넘기거나,
  `app.core.error_codes` 에서 가져온 코드를 호출한 것. 응답에서 `code_source="domain"` 이 된다.
- **status** — 그 밖의 전부(문자열 detail 등). 응답에서 `code_source="status"` 가 된다.

문구는 보지 않는다 — 문자열에서 코드를 추론하는 순간 이 트랙이 없애려는 문자열 매칭을
서버 안으로 들여오는 것이다(X4).

사용
----
::

    python -m app.core.error_codes.audit            # 지표 출력
    python -m app.core.error_codes.audit --export   # registry.generated.json 갱신
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]  # app/

# 이 이름들로 raise 하면 HTTP 응답이 나간다. `HTTPException` 은 직접 raise 하는 경우.
_HTTP_RAISE_NAMES = {"HTTPException", "AppError"}
_EXCEPTIONS_MODULE = "app.utils.exceptions"
_CODES_MODULE_PREFIX = "app.core.error_codes"


@dataclass
class Site:
    """`raise` 한 지점."""

    path: str
    line: int
    kind: str  # "domain" | "status"
    code: str | None


def _imported_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(예외 클래스 이름, 레지스트리 코드 이름) — 파일 단위로 모은다."""
    exc_names: set[str] = set(_HTTP_RAISE_NAMES)
    code_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if node.module == _EXCEPTIONS_MODULE:
            exc_names.update(a.asname or a.name for a in node.names)
        elif node.module.startswith(_CODES_MODULE_PREFIX):
            code_names.update(a.asname or a.name for a in node.names)
    return exc_names, code_names


def _detail_has_code(call: ast.Call) -> str | None:
    """`detail={"code": ...}` 또는 `code=...` 에서 코드 리터럴을 뽑는다.

    - 리터럴이면 그 문자열.
    - 상수 참조(`CODE_ALREADY_CONFIRMED`, `codes.SCHEDULE_INVALID`)는 `"<indirect>"` —
      이미 상수로 묶여 있으므로 문제는 아니다.
    - f-string 조립(`f"{field}_taken"`)은 `"<assembled>"` — **정적으로 알 수 없어
      레지스트리 보호(G2/G4) 밖이다.** 그 자체가 발견 항목이라 따로 표시한다.
    """
    for kw in call.keywords:
        value: ast.expr | None = None
        if kw.arg == "code":
            value = kw.value
        elif kw.arg == "detail" and isinstance(kw.value, ast.Dict):
            for key, item in zip(kw.value.keys, kw.value.values):
                if isinstance(key, ast.Constant) and key.value == "code":
                    value = item
                    break
        if value is None:
            continue
        if isinstance(value, ast.Constant):
            return str(value.value)
        if isinstance(value, (ast.Name, ast.Attribute)):
            return "<indirect>"
        return "<assembled>"
    return None


def scan(root: Path | None = None) -> list[Site]:
    """`app/` 전체의 raise 지점을 분류한다."""
    base = root or APP_ROOT
    sites: list[Site] = []
    for path in sorted(base.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        exc_names, code_names = _imported_names(tree)
        rel = str(path.relative_to(base.parent))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            call = node.exc
            func = call.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else ""
            )
            if name in code_names:
                sites.append(Site(rel, node.lineno, "domain", name))
                continue
            if name not in exc_names:
                continue
            code = _detail_has_code(call)
            if code is not None:
                sites.append(Site(rel, node.lineno, "domain", code))
            else:
                sites.append(Site(rel, node.lineno, "status", None))
    return sites


def summary(sites: list[Site]) -> dict[str, int]:
    counts = Counter(s.kind for s in sites)
    return {
        "total": len(sites),
        "domain": counts.get("domain", 0),
        "status": counts.get("status", 0),
    }


_PLACEHOLDERS = frozenset({"<indirect>", "<assembled>"})


def unregistered_codes(sites: list[Site]) -> set[str]:
    """raise 되지만 선언되지 않은 코드 — 있으면 레지스트리가 실태를 덜 담고 있다는 뜻.

    상수 참조/f-string 조립 지점은 정적으로 값을 알 수 없어 제외한다.
    """
    from app.core.error_codes import all_codes

    declared = set(all_codes())
    return {
        s.code
        for s in sites
        if s.kind == "domain"
        and s.code
        and s.code not in _PLACEHOLDERS
        and s.code not in declared
    }


def _print_report(sites: list[Site]) -> None:
    from app.core.error_codes import all_codes, all_domains

    stat = summary(sites)
    pct = (100.0 * stat["status"] / stat["total"]) if stat["total"] else 0.0
    print(f"raise sites      : {stat['total']}")
    print(f"  code_source=domain : {stat['domain']}")
    print(f"  code_source=status : {stat['status']}  ({pct:.1f}%)   <- 이 숫자가 줄어야 한다")
    print()
    print(f"registered codes : {len(all_codes())} in {len(all_domains())} domains")

    unknown = sorted(unregistered_codes(sites))
    if unknown:
        print()
        print("raise 되지만 레지스트리에 없는 코드 (선언을 추가할 것):")
        for code in unknown:
            print(f"  - {code}")
    assembled = [s for s in sites if s.code == "<assembled>"]
    if assembled:
        print()
        print("코드를 문자열로 조립하는 지점 (레지스트리 보호 밖):")
        for site in assembled:
            print(f"  - {site.path}:{site.line}")

    print()
    print("status 가 많은 파일 top 15:")
    worst = Counter(s.path for s in sites if s.kind == "status")
    for path, n in worst.most_common(15):
        print(f"  {n:4d}  {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--export",
        action="store_true",
        help="registry.generated.json 을 다시 쓴다 (3-repo 대조용).",
    )
    args = parser.parse_args(argv)

    if args.export:
        from app.core.error_codes import REGISTRY_JSON_PATH, registry_json

        REGISTRY_JSON_PATH.write_text(registry_json(), encoding="utf-8")
        print(f"wrote {REGISTRY_JSON_PATH}")
        return 0

    _print_report(scan())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
