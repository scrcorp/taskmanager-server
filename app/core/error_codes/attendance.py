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
"""params: minutes_early(int), schedule_id(str), scheduled_start(ISO str).

⚠️ params 는 **detail 최상위에 평탄하게** 실린다. 한 겹 감싸면 구버전이 `minutes_early` 를
못 찾아 "0m early" 로 틀린 값을 맞는 것처럼 표시한다(X3).
"""

ACCESS_CODE_TAKEN = ATTENDANCE.legacy(
    "access_code_taken",
    409,
    "This code is already used by another organization. Choose a different code.",
    frozen=True,
    clients=("console/src/hooks/useAttendanceDevices.ts",),
)
