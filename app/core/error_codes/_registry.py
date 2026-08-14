"""에러 코드 레지스트리의 뼈대 — 코드 하나를 "선언"하는 유일한 방법.

왜 레지스트리가 필요한가
------------------------
2026-08-11 전수 조사 기준 서버에는 코드 문자열이 82개 있는데 그중 **57개가 상수 없이
호출부 리터럴**이었다. 그래서 두 도메인이 같은 문자열을 다른 뜻으로 쓰고 있어도
**아무도 몰랐다** (실제로 `store_not_found` 가 가입 플로우와 매장 사진 업로드에서,
`file_too_large` 가 지원서 첨부와 매장 사진에서 각각 쓰이고 있었다).

여기서 강제하는 것 (계약안 G2·G4·G6)
------------------------------------
- **G2 — 미등록 코드 생성 불가**: 코드는 `Domain.code()` / `.legacy()` / `.item()` 으로만
  만들어진다. 오타는 런타임 응답이 아니라 **모듈 import 시점**에 터진다.
- **G4 — 전역 중복 검사**: 같은 코드 문자열을 두 곳에서 선언하면 import 시 `ValueError`.
  공용으로 쓰는 코드는 `common` 도메인에 **한 번만** 선언하고 여러 도메인이 가져다 쓴다.
- **G6 — 접근성 대칭**: `raise NotFoundError("...")` 가 한 줄이라 아무도
  `AppError(status_code=…, code=…, message=…)` 를 안 썼다(2026-06-22 도입 후 2개월간 실사용 1건).
  그래서 코드 객체 자체를 호출 가능하게 만들었다 — `raise SIGNUPS_PAUSED()` 한 줄이면 끝이고,
  status/message/hint 는 선언부에 이미 적혀 있다.

표기 규칙 (계약안 E3)
---------------------
- **신규 코드는 `UPPER_SNAKE`** — `code()` 가 형식을 강제한다.
- 이미 쓰이고 있는 `lower_snake` 는 `legacy()` 로만 선언할 수 있다.
  그중 3-repo(console/HTMA/staff)에 이미 배포되어 **개명하면 구버전 클라가 죽는** 것은
  `frozen=True` + `clients=(...)` 로 못 박는다. X2 — 이 8건은 영원히 개명 금지다.
  "일관성"을 이유로 UPPER 로 바꾸는 순간 구버전 HTMA 의 조기 출근이 멈춘다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.utils.exceptions import AppError

# UPPER_SNAKE: 대문자/숫자, 밑줄 구분. 한 글자 코드는 실수일 가능성이 커서 막는다.
_UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$")
_LOWER_SNAKE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")

# 봉투가 자체 필드로 흡수하거나 화이트리스트로 최상위 유지하는 이름.
# params 로 이 이름을 쓰면 detail/error 양쪽에서 의미가 겹쳐 조용히 덮어써진다.
_RESERVED_PARAM_NAMES = frozenset(
    {"code", "message", "hint", "errors", "warnings", "retry", "params"}
)


@dataclass(frozen=True)
class ErrorCode:
    """코드 하나. **선언 = 계약**이다 — status·문구·hint 가 여기 한 곳에만 있다.

    두 가지 쓰임이 있다.

    1. HTTP 에러 (`kind="http"`) — 그대로 raise 한다::

           raise SIGNUPS_PAUSED()
           raise PIN_CONFLICT(reason="exact", conflicting_empid="1043")

    2. 검증 항목 (`kind="item"`) — 응답 하나에 여러 개가 `errors`/`warnings` 배열로 실린다.
       스케줄 검증이 이 모양이다. `issue()` 로 만든다.
    """

    code: str
    domain: str
    kind: str  # "http" | "item"
    status_code: int | None
    message: str
    hint: str | None = None
    frozen: bool = False
    """3-repo 에 배포되어 개명 불가(X2). 왜 그런지는 `clients` 에 적는다."""
    clients: tuple[str, ...] = ()
    """이 코드를 **분기 조건으로 읽는** 클라 지점. 개명·삭제 전에 여기부터 본다."""

    def __call__(
        self,
        *,
        message: str | None = None,
        hint: str | None = None,
        **params: Any,
    ) -> AppError:
        """이 코드로 던질 예외를 만든다 — `raise CODE(...)`.

        `detail` 은 **평탄 구조**로 만든다(`{"code", "message", ...params}`).
        params 를 한 겹 감싸면 구버전 HTMA 가 `minutes_early` 를 못 찾아 "0m early" 처럼
        **틀린 값을 맞는 것처럼** 표시한다 — 정보 소실보다 나쁘다(X3).
        봉투의 `error.params` 는 전역 핸들러가 이 평탄 detail 에서 파생시킨다.
        """
        if self.kind != "http":
            raise TypeError(
                f"{self.code} is a validation item, not an HTTP error. "
                f"Use {self.code}.issue(...) and put it in errors/warnings."
            )
        bad = _RESERVED_PARAM_NAMES & set(params)
        if bad:
            raise ValueError(
                f"{self.code}: params {sorted(bad)} collide with envelope fields."
            )
        assert self.status_code is not None
        return AppError(
            status_code=self.status_code,
            code=self.code,
            message=message or self.format(**params),
            hint=hint if hint is not None else self.hint,
            params=params or None,
        )

    def issue(self, **params: Any) -> dict[str, Any]:
        """검증 항목 하나를 계약 형태 `{"code", "params"}` 로 만든다.

        `None` 은 싣지 않는다 — 클라가 "키가 있으면 값이 있다"고 가정할 수 있게(참조 구현 승계).
        """
        clean = {k: v for k, v in params.items() if v is not None}
        return {"code": self.code, "params": clean}

    def format(self, **params: Any) -> str:
        """fallback 문구. `{step_minutes}` 같은 자리표시자를 params 로 채운다.

        채울 값이 없으면 **템플릿 원문을 그대로** 낸다 — 문구를 만들다 실패해서
        에러 표시 자체가 사라지는 일이 없어야 한다.
        """
        try:
            return self.message.format(**params)
        except (KeyError, IndexError):
            return self.message

    def as_dict(self) -> dict[str, Any]:
        """3-repo 대조용 직렬화 (G3). 필드 이름은 클라 테스트가 읽는 계약이다."""
        return {
            "code": self.code,
            "domain": self.domain,
            "kind": self.kind,
            "status": self.status_code,
            "message": self.message,
            "hint": self.hint,
            "frozen": self.frozen,
            "clients": list(self.clients),
        }


# 코드 문자열 → 선언. **전역 유일**이어야 한다(G4).
_BY_CODE: dict[str, ErrorCode] = {}
_DOMAINS: dict[str, "Domain"] = {}


@dataclass
class Domain:
    """한 도메인의 코드 묶음. 도메인 파일 하나가 `Domain` 하나를 만든다."""

    name: str
    codes: dict[str, ErrorCode] = field(default_factory=dict)

    def _add(self, entry: ErrorCode) -> ErrorCode:
        existing = _BY_CODE.get(entry.code)
        if existing is not None:
            # 같은 뜻으로 여러 도메인이 쓰는 코드라면 `common` 에 한 번만 선언하고 import 할 것.
            raise ValueError(
                f"Duplicate error code {entry.code!r}: already declared in domain "
                f"{existing.domain!r}, re-declared in {entry.domain!r}. "
                f"If both mean the same thing, declare it once in the 'common' domain."
            )
        _BY_CODE[entry.code] = entry
        self.codes[entry.code] = entry
        return entry

    def code(
        self,
        code: str,
        status_code: int,
        message: str,
        *,
        hint: str | None = None,
    ) -> ErrorCode:
        """신규 코드 — `UPPER_SNAKE` 만 허용한다."""
        if not _UPPER_SNAKE.match(code):
            raise ValueError(
                f"New error codes must be UPPER_SNAKE: {code!r}. "
                f"Existing lower_snake codes must be declared with legacy()."
            )
        return self._add(
            ErrorCode(
                code=code,
                domain=self.name,
                kind="http",
                status_code=status_code,
                message=message,
                hint=hint,
            )
        )

    def legacy(
        self,
        code: str,
        status_code: int,
        message: str,
        *,
        hint: str | None = None,
        frozen: bool = False,
        clients: Iterable[str] = (),
    ) -> ErrorCode:
        """이미 쓰이고 있는 `lower_snake` 코드.

        `frozen=True` 는 **3-repo 에 배포되어 개명 불가**를 뜻한다(X2).
        `clients` 에는 그 코드를 분기 조건으로 읽는 클라 파일을 적는다 — 근거 없는 frozen 은
        나중에 아무도 해제하지 못한다.
        """
        if not _LOWER_SNAKE.match(code) and not _UPPER_SNAKE.match(code):
            raise ValueError(f"Unrecognized code shape: {code!r}")
        if frozen and not tuple(clients):
            raise ValueError(
                f"{code!r}: frozen=True requires clients=(...) — "
                f"record where it is read, or it can never be un-frozen."
            )
        return self._add(
            ErrorCode(
                code=code,
                domain=self.name,
                kind="http",
                status_code=status_code,
                message=message,
                hint=hint,
                frozen=frozen,
                clients=tuple(clients),
            )
        )

    def item(
        self,
        code: str,
        message: str,
        *,
        frozen: bool = False,
        clients: Iterable[str] = (),
    ) -> ErrorCode:
        """검증 항목 코드 — 단독 status 가 없고 `errors`/`warnings` 배열에 실린다."""
        if not _UPPER_SNAKE.match(code):
            raise ValueError(f"Validation item codes must be UPPER_SNAKE: {code!r}")
        return self._add(
            ErrorCode(
                code=code,
                domain=self.name,
                kind="item",
                status_code=None,
                message=message,
                frozen=frozen,
                clients=tuple(clients),
            )
        )


def domain(name: str) -> Domain:
    """도메인을 만든다(또는 이미 있으면 그것을 돌려준다)."""
    existing = _DOMAINS.get(name)
    if existing is not None:
        return existing
    created = Domain(name=name)
    _DOMAINS[name] = created
    return created


def all_codes() -> dict[str, ErrorCode]:
    """코드 문자열 → 선언 (읽기용 사본)."""
    return dict(_BY_CODE)


def all_domains() -> dict[str, Domain]:
    return dict(_DOMAINS)


def get(code: str) -> ErrorCode | None:
    return _BY_CODE.get(code)


def fallback_message(code: str, **params: Any) -> str:
    """코드에 대응하는 문구. 미등록이면 **코드 원문**을 그대로 돌려준다.

    일반 문구("Bad request.")로 덮지 않는 이유 — 그럴듯한 문장이 뜨면 아무도 그 지점을
    고치지 않는다(E1-d). 코드가 그대로 보이면 못생겼지만 추적은 가능하다.
    참조 구현 `schedule_codes.fallback_message()` 와 같은 원칙이다.
    """
    entry = _BY_CODE.get(code)
    if entry is None:
        return code
    return entry.format(**params)
