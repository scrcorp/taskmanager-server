"""근태 / 키오스크(HTMA) — **가장 되돌리기 어려운 코드들**.

HTMA 는 사이드로드 APK 라 배포된 구버전이 현장에 계속 남는다. 아래 두 코드는 앱이
**분기 조건**으로 읽으며, 개명하면 서버 롤백 외에 되돌릴 방법이 없다(X2).

- `early_clock_in_reason_required` — 앱이 이 코드를 못 잡으면 사유 시트를 못 띄우는데
  서버는 계속 사유를 요구한다 → **조기 출근 영구 실패(데드락)**.
- `pin_conflict` — console/HTMA/staff 3종이 모두 읽는다.
"""

from __future__ import annotations

from app.core.error_codes._registry import domain

ATTENDANCE = domain("attendance")

PIN_CONFLICT = ATTENDANCE.legacy(
    "pin_conflict",
    409,
    "This PIN is already in use by another employee.",
    hint="Choose a different PIN.",
    frozen=True,
    clients=(
        "console/src/hooks/useClockinPin.ts",
        "app/apps/attendance/lib/screens/attendance/attendance_manage_staff_pins_screen.dart",
        "app/apps/staff/lib/services/clockin_pin_service.dart",
    ),
)
"""params: reason("exact"), other_store(bool|None).

reason 은 exact 하나뿐 — 앞자리가 겹치는 다른 길이의 PIN 은 2026-08-13 부터 허용한다
(구버전 클라이언트의 "prefix" 분기는 더 이상 발사되지 않는다).

타인의 PIN 값·이름은 어떤 필드에도 싣지 않는다 — 키오스크 화면은 고객 눈에도 띈다.
"""

EARLY_CLOCK_IN_REASON_REQUIRED = ATTENDANCE.legacy(
    "early_clock_in_reason_required",
    400,
    "This shift starts in {minutes_early} minutes. To clock in now, enter a reason — "
    "your manager will see it.",
    frozen=True,
    clients=(
        "app/apps/attendance/lib/utils/early_clock_in_logic.dart",
        "app/apps/attendance/lib/widgets/early_clock_in_dialog.dart",
    ),
)
"""params: minutes_early(int), schedule_id(str), scheduled_start(ISO str),
scheduled_start_display(str, "Aug 19, 5:00 PM" — 날짜 포함 라벨).

⚠️ params 는 **detail 최상위에 평탄하게** 실린다. 한 겹 감싸면 구버전이 `minutes_early` 를
못 찾아 "0m early" 로 틀린 값을 맞는 것처럼 표시한다(X3).
"""

OVERLAPPING_CLOCK_IN_CONFIRMATION_REQUIRED = ATTENDANCE.legacy(
    "overlapping_clock_in_confirmation_required",
    400,
    "You are still clocked in to another shift. Clock in anyway?",
    clients=("app/apps/attendance/lib/screens/attendance/attendance_main_screen.dart",),
)
"""params: open_attendance_ids(list[str]), open_schedule_ids(list[str]),
open_scheduled_start_display / open_scheduled_end_display("HH:mm"|None).

`early_clock_in_reason_required` 와 **같은 재시도 형태**다 — 앱이 경고를 띄우고
같은 요청에 `allow_overlap: true` 만 붙여 다시 보낸다. 상태기계에 새 개념이 생기지
않게 일부러 이 모양으로 맞췄다.

`lower_snake` 인 이유: 이 코드를 읽는 곳이 조기 출근 사유 시트와 같은 HTMA clock-in
흐름이고, 한 흐름 안에서 코드 표기가 갈리면 앱 분기 코드가 두 규칙을 들고 있어야 한다.
"""

SHIFT_NOT_AVAILABLE = ATTENDANCE.legacy(
    "shift_not_available",
    400,
    "That shift is no longer available for clock-in. Pick another one.",
    hint="Re-identify with your PIN to see the current list of shifts.",
    clients=("app/apps/attendance/lib/screens/attendance/attendance_main_screen.dart",),
)
"""params: schedule_id(str).

기존 문자열 400 "Selected shift is not available for clock-in" 을 구조화한 것.
앱이 이 코드를 잡으면 identify 를 다시 호출해 picker 를 갱신할 수 있다 — 문자열이면
"왜 안 되는지" 만 알고 "그다음 무엇을 할지" 는 알 수 없다.
"""

DEVICE_NO_STORE = ATTENDANCE.code(
    "DEVICE_NO_STORE",
    400,
    "This device is not assigned to a store yet.",
    hint="Ask a manager to pick a store in the device settings.",
)
"""params: 없음.

같은 상황을 `tip.py`/`manage.py` 는 아직 문자열 400 "Device has no store assigned" 로
던진다. 그쪽 전환은 별건(구버전 클라의 표시 문구가 바뀐다) — **신규 엔드포인트만**
이 코드로 시작한다. 새 엔드포인트를 읽는 구버전은 존재하지 않으므로 여기서 시작하는
비용이 0 이다.
"""

INVALID_REASON_USER = ATTENDANCE.legacy(
    "invalid_reason_user",
    400,
    "That person is no longer on this store's manager list.",
    hint="Submit again without picking a person, or pick someone from the current list.",
    clients=("app/apps/attendance/lib/widgets/early_clock_in_dialog.dart",),
)
"""params: 없음.

조용한 null 저장이 아니라 **400** 인 이유: 앱이 방금 서버에서 받은 명단에서 고른 값이므로
불일치는 클라 버그이거나 변조다. 조용히 버리면 이 컬럼을 만든 이유(동명이인·개명 추적)가
그대로 사라진다("조용한 실패 금지").

동시에 앱은 이 코드를 받으면 **id 를 떼고 같은 사유 문자열로 1회 자동 재시도**한다 —
키오스크 앞에서 정당한 조기 출근이 막히지 않아야 하기 때문. 두 규칙("조용한 실패 금지" /
"현장 차단 금지")을 동시에 만족시키는 유일한 조합이다.

`lower_snake` 인 이유: 같은 clock-in 흐름의 `early_clock_in_reason_required` 와 표기를
맞춘다. 한 흐름 안에서 코드 표기가 갈리면 앱 분기가 두 규칙을 들고 있어야 한다.
"""

ACCESS_CODE_TAKEN = ATTENDANCE.legacy(
    "access_code_taken",
    409,
    "This code is already used by another organization. Choose a different code.",
    frozen=True,
    clients=("console/src/hooks/useAttendanceDevices.ts",),
)
