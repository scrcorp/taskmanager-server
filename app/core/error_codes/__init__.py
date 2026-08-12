"""에러 코드 레지스트리 — **코드 문자열이 존재하는 유일한 장소**.

쓰는 법 (이게 전부다)
---------------------
::

    from app.core.error_codes.signup import SIGNUPS_PAUSED

    raise SIGNUPS_PAUSED()

status·문구·hint 는 선언부에 이미 있다. `raise NotFoundError("...")` 와 길이가 같아야
실제로 쓰인다 — 이 대칭이 없으면(G6) 아래 강제 장치가 다 있어도 신규 코드는 계속
문자열 raise 로 간다. 2026-06-22 `AppError` 가 정확히 그렇게 사문화됐다.

부가 데이터를 실을 때::

    raise PIN_CONFLICT(reason="prefix", other_store=False)

`detail` 은 `{"code", "message", "reason", "other_store"}` 처럼 **평탄**하게 나간다.
구버전 클라가 최상위에서 그 값을 읽기 때문이다(X3).

새 코드를 추가하려면
--------------------
1. 도메인 파일(`signup.py`, `attendance.py`, …)에 한 줄 선언한다. 신규는 `UPPER_SNAKE`.
   새 도메인이면 파일을 만들고 아래 `_DOMAIN_MODULES` 에 등록한다 — 등록하지 않으면
   전역 중복 검사(G4)와 3-repo 덤프(G3)에서 빠진다.
2. `raise` 지점에서 그 코드를 호출한다.
3. `python -m app.core.error_codes.audit --export` 로 덤프를 갱신한다(테스트가 확인한다).
4. 클라가 이 코드로 **분기**한다면 선언에 `clients=(...)` 를 적는다. 나중에 개명·삭제할
   사람이 볼 유일한 근거다.

전역 핸들러(`app/core/error_envelope.py`)는 `detail` 의 `code` 를 **그대로** 신뢰한다.
따라서 이 레지스트리를 쓰면 응답의 `error.code_source` 가 자동으로 `"domain"` 이 된다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.error_codes._registry import (
    Domain,
    ErrorCode,
    all_codes,
    all_domains,
    domain,
    fallback_message,
    get,
)

# 도메인 모듈을 **여기서 전부 import** 한다 — import 되지 않은 도메인은 존재하지 않는
# 것과 같아서 중복 검사도 덤프도 통과해버린다. 새 도메인 파일을 만들면 반드시 추가할 것.
from app.core.error_codes import access as _access  # noqa: F401,E402
from app.core.error_codes import attendance as _attendance  # noqa: F401,E402
from app.core.error_codes import common as _common  # noqa: F401,E402
from app.core.error_codes import hiring as _hiring  # noqa: F401,E402
from app.core.error_codes import interviews as _interviews  # noqa: F401,E402
from app.core.error_codes import payroll as _payroll  # noqa: F401,E402
from app.core.error_codes import schedule as _schedule  # noqa: F401,E402
from app.core.error_codes import signup as _signup  # noqa: F401,E402
from app.core.error_codes import store as _store  # noqa: F401,E402

_DOMAIN_MODULES = (
    _access,
    _attendance,
    _common,
    _hiring,
    _interviews,
    _payroll,
    _schedule,
    _signup,
    _store,
)

# 3-repo 대조용 산출물(G3). console/app 테스트가 이 파일을 읽어 자기 목록과 맞춘다.
REGISTRY_JSON_PATH = Path(__file__).with_name("registry.generated.json")

REGISTRY_VERSION = 1
"""덤프 형식 버전. 필드를 빼거나 뜻을 바꾸면 올린다 — 클라 테스트가 이 값을 확인한다."""


def export_registry() -> dict[str, Any]:
    """레지스트리 전체를 직렬화한다 — 3-repo 목록 일치 테스트(G3)의 서버 측 산출물.

    도메인이 늘어나면 자동으로 실린다. 스케줄에만 있던 대조 테스트를 일반화하는 것이
    목적이므로, 클라는 도메인별 코드 집합만 보고 자기 매핑을 확장하면 된다.
    """
    codes = sorted(all_codes().values(), key=lambda e: (e.domain, e.code))
    return {
        "version": REGISTRY_VERSION,
        "generated_by": "python -m app.core.error_codes.audit --export",
        "domains": {name: sorted(d.codes) for name, d in sorted(all_domains().items())},
        "codes": [entry.as_dict() for entry in codes],
    }


def registry_json() -> str:
    """파일에 쓰는 것과 **바이트 동일한** 문자열. 테스트가 이걸로 최신 여부를 판단한다."""
    return json.dumps(export_registry(), indent=2, ensure_ascii=False) + "\n"


__all__ = [
    "Domain",
    "ErrorCode",
    "REGISTRY_JSON_PATH",
    "REGISTRY_VERSION",
    "all_codes",
    "all_domains",
    "domain",
    "export_registry",
    "fallback_message",
    "get",
    "registry_json",
]
