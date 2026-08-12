"""스케줄 검증의 에러/경고 코드 — **세 저장소가 공유하는 계약의 서버 측 단일 출처**.

계약 (D9)
---------
서버는 **코드 + 파라미터**를 반환하고, 문구는 클라이언트가 구성한다.
`message` 는 fallback 전용이며 클라이언트가 문자열을 매칭해서는 안 된다.

    400  {"code": "SCHEDULE_INVALID",  "message": "...", "errors": [...], "warnings": [...]}
    409  {"code": "SCHEDULE_WARNINGS_UNCONFIRMED", "message": "...",
          "warnings": [...], "retry": {"force": true}}

`errors`/`warnings` 각 항목: `{"code": "<UPPER_SNAKE>", "params": {...}}`

분류 기준 (D9)
-------------
**"데이터가 깨지는가."**

- **에러** — 그 자체로 무의미한 데이터. 차단하며 `force` 로도 넘을 수 없다.
- **경고** — 현실에서 일어날 수 있는 일. **확인 후 진행**(409 → `force:true` 재요청).

겹침(OVERLAPPING_SCHEDULE)이 에러가 아니라 경고인 이유: 한 사람이 두 역할을
겹쳐 맡는 상황이 실제로 있을 수 있는데 지금은 표현할 방법이 없었다.

표기
----
`UPPER_SNAKE`. 기존 3건(`pin_conflict`, `store_closed`, `early_clock_in_reason_required`)은
이미 3-repo 에 배포된 클라이언트 계약이라 건드리지 않는다 — 바꾸려면 세 저장소를
동시에 고쳐야 하고 구버전 HTMA 가 깨진다.

주의 — 이름이 비슷한 별개 개념
------------------------------
- `store_inactive` (기존 409, 매장 비활성) ≠ `STORE_CLOSED_DAY` (신규 경고, 휴무일)
"""

from __future__ import annotations

from typing import Any

# ── 응답 최상위 코드 ────────────────────────────────────────

SCHEDULE_INVALID = "SCHEDULE_INVALID"
"""400 — 에러가 하나 이상. force 로도 넘을 수 없다."""

SCHEDULE_WARNINGS_UNCONFIRMED = "SCHEDULE_WARNINGS_UNCONFIRMED"
"""409 — 경고만 있고 아직 확인받지 않았다. `force:true` 로 재요청하면 저장된다."""


# ── 에러 (차단, force 무효) ─────────────────────────────────

ZERO_DURATION = "ZERO_DURATION"
"""근무 길이가 0분 이하. params: {start_at, end_at}"""

START_DATE_OUT_OF_WINDOW = "START_DATE_OUT_OF_WINDOW"
"""시작 달력일이 영업일·+1일 밖. params: {operating_day, start_date}"""

USER_NOT_IN_STORE = "USER_NOT_IN_STORE"
"""해당 매장에 배정되지 않은 직원. params: {user_id, store_id}"""

USER_NOT_MARKED_FOR_STORE = "USER_NOT_MARKED_FOR_STORE"
"""매장 근무 대상으로 표시되지 않은 직원. params: {user_id, store_id}"""

TIME_NOT_ON_GRID = "TIME_NOT_ON_GRID"
"""시각이 입력 단위를 벗어남. params: {field, value, step_minutes}"""

BREAK_PAIR_INCOMPLETE = "BREAK_PAIR_INCOMPLETE"
"""휴게 시작/종료 중 하나만 있음. params: {missing}"""

BREAK_REVERSED = "BREAK_REVERSED"
"""휴게 종료가 시작보다 이르거나 같음 — 음수 휴게는 net 을 오염시킨다.
params: {break_start_at, break_end_at}"""

BREAK_OUTSIDE_SHIFT = "BREAK_OUTSIDE_SHIFT"
"""휴게가 근무 시간 밖. params: {break_start_at, break_end_at, start_at, end_at}"""

PAY_PERIOD_LOCKED = "PAY_PERIOD_LOCKED"
"""확정된 급여 기간. params: {work_date, direction: "into"|"out_of"}"""


# ── 경고 (확인 후 진행) ─────────────────────────────────────

OVERLAPPING_SCHEDULE = "OVERLAPPING_SCHEDULE"
"""같은 직원의 다른 스케줄과 시간이 겹침. params: {user_id}"""

SHIFT_TOO_LONG = "SHIFT_TOO_LONG"
"""max_shift_hours 초과. params: {net_minutes, limit_hours}"""

WEEKLY_OVERTIME = "WEEKLY_OVERTIME"
"""주간 근무시간 한도 초과. params: {total_minutes, limit_minutes}"""

START_AFTER_DAY_BOUNDARY = "START_AFTER_DAY_BOUNDARY"
"""+1일로 지정됐는데 시작이 경계 이후 — 다음 영업일 소속일 수 있다.
params: {boundary, start_date}"""

START_BEFORE_DAY_BOUNDARY = "START_BEFORE_DAY_BOUNDARY"
"""당일로 지정됐는데 시작이 경계 이전 — 전 영업일에 속하는 시각이다.
params: {boundary, start_date}"""

STORE_CLOSED_DAY = "STORE_CLOSED_DAY"
"""휴무일에 스케줄을 만든다 (D2-6: 허용하되 경고). params: {weekday}"""

OPERATING_DAY_OVERRIDDEN = "OPERATING_DAY_OVERRIDDEN"
"""자동 판정과 다른 영업일이 명시됨 (D3-3). params: {auto, chosen}"""


ERROR_CODES: frozenset[str] = frozenset({
    ZERO_DURATION,
    START_DATE_OUT_OF_WINDOW,
    USER_NOT_IN_STORE,
    USER_NOT_MARKED_FOR_STORE,
    TIME_NOT_ON_GRID,
    BREAK_PAIR_INCOMPLETE,
    BREAK_REVERSED,
    BREAK_OUTSIDE_SHIFT,
    PAY_PERIOD_LOCKED,
})

WARNING_CODES: frozenset[str] = frozenset({
    OVERLAPPING_SCHEDULE,
    SHIFT_TOO_LONG,
    WEEKLY_OVERTIME,
    START_AFTER_DAY_BOUNDARY,
    START_BEFORE_DAY_BOUNDARY,
    STORE_CLOSED_DAY,
    OPERATING_DAY_OVERRIDDEN,
})


def issue(code: str, **params: Any) -> dict[str, Any]:
    """검증 항목 하나를 계약 형태로 만든다.

    None 파라미터는 싣지 않는다 — 클라이언트가 "키가 있으면 값이 있다"고
    가정할 수 있게 하기 위함.
    """
    if code not in ERROR_CODES and code not in WARNING_CODES:
        raise ValueError(f"Unregistered schedule code: {code}")
    clean = {k: v for k, v in params.items() if v is not None}
    return {"code": code, "params": clean}


# ── fallback 문구 ───────────────────────────────────────────
# 클라이언트가 code 를 모를 때만 쓰는 최후 수단. **문자열 매칭 금지.**

_FALLBACK: dict[str, str] = {
    ZERO_DURATION: "Shift duration must be greater than 0 minutes.",
    START_DATE_OUT_OF_WINDOW: "Start date must be on the operating day or the next day.",
    USER_NOT_IN_STORE: "This employee is not assigned to this store.",
    USER_NOT_MARKED_FOR_STORE: "This employee is not marked for work at this store.",
    TIME_NOT_ON_GRID: "Time must be in {step_minutes}-minute increments.",
    BREAK_PAIR_INCOMPLETE: "Break start and end must be provided together.",
    BREAK_REVERSED: "Break end must be after break start.",
    BREAK_OUTSIDE_SHIFT: "Break must fall within the shift.",
    PAY_PERIOD_LOCKED: "This pay period is locked.",
    OVERLAPPING_SCHEDULE: "This employee already has an overlapping schedule.",
    SHIFT_TOO_LONG: "This shift is longer than the configured limit.",
    WEEKLY_OVERTIME: "Weekly hours exceed the configured limit.",
    START_AFTER_DAY_BOUNDARY: "Start is at or after the store's day boundary.",
    START_BEFORE_DAY_BOUNDARY: "Start is before the store's day boundary.",
    STORE_CLOSED_DAY: "The store is closed on this day.",
    OPERATING_DAY_OVERRIDDEN: "The operating day differs from the automatic result.",
}


def fallback_message(items: list[dict[str, Any]]) -> str:
    """구버전 클라이언트용 한 문장. 새 클라이언트는 code 로 문구를 구성한다."""
    if not items:
        return "Validation failed."
    parts = []
    for it in items:
        tpl = _FALLBACK.get(it["code"], it["code"])
        try:
            parts.append(tpl.format(**it.get("params", {})))
        except (KeyError, IndexError):
            parts.append(tpl)
    return " ".join(parts)
