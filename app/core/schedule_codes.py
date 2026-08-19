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

START_DATE_MISMATCH = "START_DATE_MISMATCH"
"""시작 달력일이 **자동 판정과 다른데 사용자가 고른 것이라는 표시가 없다**.

경계(day_start)와 시작 시각이 정해지면 시작 달력일은 하나로 결정된다
(`operating_day + (시작 < 경계 ? 1 : 0)`). 그 값과 다른 날짜가 들어오는 경우는 둘뿐이다:
사람이 화면에서 **직접 고른 것**이거나, 클라이언트가 옛 오프셋을 그대로 실어 보낸 **버그**다.
**확인으로도 넘길 수 없다.** 자동값과 다른 시작 달력일은 예외 없이 자기 영업일 구간 밖이고
(구간이 반열림이라 두 후보 중 하나만 안에 든다), 그런 행은 저장돼도 현장에서 쓸 수 없다 —
출근하려는 시각의 영업일과 라벨이 달라 후보 조회에 안 잡히고, 경계가 지난 뒤엔 이미 끝난
근무다. 2026-08 의 "1439분 조기출근" 오탐이 이 상태였고, 당시엔 경고뿐이라 벌크 경로의
force 에 삼켜져 24건이 저장됐다.

의도가 "그 달력일에 일한다" 라면 바꿔야 할 것은 시작 날짜가 아니라 **영업일**이다 —
`suggested_operating_day` 가 그 값을 담는다.

params: {auto, chosen, boundary, start_time, operating_day, suggested_operating_day}"""

SHIFT_SPAN_TOO_LONG = "SHIFT_SPAN_TOO_LONG"
"""근무 구간이 24시간을 넘는다 — 날짜 조립이 틀렸다는 뜻이다.

`max_shift_hours`(경고) 와 다르다. 그쪽은 "길지만 있을 수 있는 근무"이고, 이쪽은
시작·종료 달력일 조합이 애초에 성립하지 않는 상태다. 종료 달력일 후보가 `시작일`/`시작일+1`
둘뿐인 것도 같은 이유다. params: {span_minutes, start_at, end_at}"""

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

# START_AFTER_DAY_BOUNDARY / START_BEFORE_DAY_BOUNDARY 는 2026-08-19 에 제거됐다.
# "자동값과 다른 시작 달력일" 은 예외 없이 자기 영업일 구간 밖이라 경고가 아니라 에러이고
# (START_DATE_MISMATCH), 그 코드가 경계·시각·권장 영업일을 모두 params 로 싣는다.
# 두 코드는 더 이상 어디서도 발생하지 않아 3-repo 계약에서 함께 지웠다.

STORE_CLOSED_DAY = "STORE_CLOSED_DAY"
"""휴무일에 스케줄을 만든다 (D2-6: 허용하되 경고). params: {weekday}"""

END_AFTER_NEXT_DAY_START = "END_AFTER_NEXT_DAY_START"
"""종료가 **다음 영업일 경계를 넘는다** — 근무의 뒷부분이 다음 영업일 창에 들어간다.

영업일 D 의 창은 `[day_start(D), day_start(D+1))` 이다. 종료가 그 끝을 넘으면 근무 시간의
일부가 D+1 에 속하는데, 스케줄 라벨은 D 하나뿐이라 **급여·리포트가 전부 D 로 귀속**된다.
막지는 않는다 — 실제로 가능한 근무이고, 막으면 표현할 방법이 사라진다. 다만 대개는
영업일을 잘못 골랐거나 매장 경계 설정이 근무 패턴과 안 맞는다는 신호라 확인을 받는다.

경계는 보통 매장이 닫혀 있는 시각으로 잡는다(04:00, 11:00 …). 이 경고가 자주 뜨면
경계 설정 자체를 다시 봐야 한다. params: {boundary, end_at, next_operating_day}"""

START_DATE_RECALCULATED = "START_DATE_RECALCULATED"
"""시각을 바꾼 결과 **시작 달력일이 함께 움직인다**. params: {from_date, to_date, boundary}

날짜를 못 보내는 구(舊) 인코딩 클라이언트(HH:mm 만 전송)를 위한 확인 장치다.
예전엔 이 경우 기존 오프셋을 그대로 보존했는데, 그게 2026-08 오염의 생성 경로였다
(09:00(+1d) shift 의 시각만 17:00 으로 바꾸면 +1d 가 남아 하루 뒤에 저장됐다).
이제는 새 시각에서 날짜를 다시 파생하되, **조용히 바꾸지 않고** 확인을 받는다.

날짜 UI 가 있는 클라이언트는 명시 `start_at` + `date_override` 를 보내므로 여기 걸리지 않는다."""

OPERATING_DAY_OVERRIDDEN = "OPERATING_DAY_OVERRIDDEN"
"""자동 판정과 다른 영업일이 명시됨 (D3-3). params: {auto, chosen}"""


ERROR_CODES: frozenset[str] = frozenset({
    ZERO_DURATION,
    START_DATE_OUT_OF_WINDOW,
    START_DATE_MISMATCH,
    SHIFT_SPAN_TOO_LONG,
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
    STORE_CLOSED_DAY,
    OPERATING_DAY_OVERRIDDEN,
    START_DATE_RECALCULATED,
    END_AFTER_NEXT_DAY_START,
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
    # 방향(경계 이전/이후)에 따라 문장이 갈리므로 fallback 은 **중립**으로 둔다.
    # 방향별 문장은 클라이언트가 code + params 로 만든다(D9-4).
    START_DATE_MISMATCH: (
        "This store's business day starts at {boundary}, so a shift counted on {operating_day} "
        "runs on {auto} when it starts at {start_time}. {chosen} falls in a different business "
        "day, where staff cannot clock in for it. To run it on {chosen}, set the operating day "
        "to {suggested_operating_day}."
    ),
    SHIFT_SPAN_TOO_LONG: "A shift cannot span more than 24 hours.",
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
    STORE_CLOSED_DAY: "The store is closed on this day.",
    OPERATING_DAY_OVERRIDDEN: "The operating day differs from the automatic result.",
    END_AFTER_NEXT_DAY_START: (
        "This shift runs past {boundary}, when the next business day ({next_operating_day}) starts. "
        "Its hours will still count on this operating day."
    ),
    START_DATE_RECALCULATED: (
        "Changing the time moves this shift from {from_date} to {to_date} "
        "(the store's day starts at {boundary})."
    ),
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
