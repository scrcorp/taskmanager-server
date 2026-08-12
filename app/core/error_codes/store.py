"""매장 — 폐점 매장 가드와 매장 사진.

이름이 비슷한 별개 개념 주의 (스케줄 쪽과 헷갈리지 말 것):
- `store_closed`   — 폐점(soft delete)된 매장. 새 데이터를 받지 않는다. **409**
- `STORE_CLOSED_DAY` — 휴무일에 스케줄을 만든다. 허용하되 경고. (schedule 도메인)
"""

from __future__ import annotations

from app.core.error_codes._registry import domain

STORE = domain("store")

STORE_CLOSED = STORE.legacy(
    "store_closed",
    409,
    "This store is closed and cannot accept new entries.",
    frozen=True,
    clients=("console/src/hooks/useSchedules.ts", "console/src/lib/scheduleCodes.ts"),
)

MAX_PHOTOS_REACHED = STORE.legacy(
    "max_photos_reached",
    400,
    "This store already has the maximum number of photos.",
    hint="Delete a photo before adding another.",
)

PHOTO_NOT_FOUND = STORE.legacy("photo_not_found", 404, "This photo no longer exists.")
