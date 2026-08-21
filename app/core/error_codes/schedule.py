"""스케줄 검증 코드 — **기존 `app/core/schedule_codes.py` 를 흡수한다(복제하지 않는다).**

`schedule_codes.py` 는 이미 3-repo(console `scheduleCodes.ts` / app `schedule_codes.dart`)에
배포된 계약이고, 그 파일이 계속 **단일 출처**다. 여기서 하는 일은 그 목록을 전역
레지스트리에 **등록**하는 것뿐이다.

왜 복사하지 않는가 — 목록을 두 벌 두면 언젠가 갈라지고, 갈라진 쪽이 어느 쪽인지
아무도 모른다. `ERROR_CODES`/`WARNING_CODES` 를 그대로 읽으므로 거기에 코드를 추가하면
여기에도 자동으로 잡히고, 3-repo 대조 덤프(G3)에도 자동으로 실린다.
"""

from __future__ import annotations

from app.core import schedule_codes as _sc
from app.core.error_codes._registry import domain

SCHEDULE = domain("schedule")

_CLIENTS = (
    "console/src/lib/scheduleCodes.ts",
    "app/apps/attendance/lib/utils/schedule_codes.dart",
)


def _template(code: str) -> str:
    """`schedule_codes` 의 fallback 문구 원본(자리표시자 포함)을 꺼낸다.

    `fallback_message()` 는 params 가 비면 `.format()` 실패를 잡아 템플릿 원문을 돌려준다 —
    그래서 private `_FALLBACK` 을 건드리지 않고도 템플릿을 얻을 수 있다.
    """
    return _sc.fallback_message([{"code": code, "params": {}}])


# ── 응답 최상위 코드 ────────────────────────────────────
SCHEDULE_INVALID = SCHEDULE.legacy(
    _sc.SCHEDULE_INVALID,
    400,
    "This schedule cannot be saved.",
    frozen=True,
    clients=_CLIENTS,
)
"""400 — `errors` 가 하나 이상. `force` 로도 넘을 수 없다."""

SCHEDULE_WARNINGS_UNCONFIRMED = SCHEDULE.legacy(
    _sc.SCHEDULE_WARNINGS_UNCONFIRMED,
    409,
    "This schedule needs confirmation before saving.",
    frozen=True,
    clients=_CLIENTS,
)
"""409 — 경고만 있고 아직 확인받지 않았다. `retry: {"force": true}` 로 재요청.

⚠️ 이 응답의 `warnings` / `retry` 는 봉투 `error` 안에서도 **최상위**를 유지한다
(화이트리스트). `params` 아래로 내려가면 겹침 확인 모달이 죽어 겹침 스케줄을 저장할 수 없다.
"""

# ── 검증 항목 (errors / warnings 배열에 실린다) ─────────
# 목록은 `schedule_codes` 가 단일 출처다. 정렬은 덤프(G3)를 안정적으로 만들기 위함.
for _code in sorted(_sc.ERROR_CODES | _sc.WARNING_CODES):
    SCHEDULE.item(_code, _template(_code), frozen=True, clients=_CLIENTS)
del _code

