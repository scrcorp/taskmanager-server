"""근태 타임라인 기록 헬퍼 — Activity History 행을 남기는 단일 진입점.

기존에는 6곳(action/device/cron service, correct_attendance, manage.py)이 각자
`AttendanceCorrection` 을 직접 만들면서 `original_value` 를 넣기도 하고 빼기도 했다.
그 결과 콘솔 Activity History 의 Before 가 대부분 비었다.

이 모듈의 계약:

1. **before 는 항상 존재한다.** 값이 비어 있었으면 `NONE` / `EMPTY` 센티널을 쓴다.
   `None` 을 그대로 저장하는 경로를 새로 만들지 말 것.
2. **한 행 = 한 항목의 전이.** `field_name` 은 바뀐 대상("status", "clock_in" …),
   `action` 은 카드 태그("clock_in", "modify" …). 둘을 섞지 않는다.
3. **한 사용자 액션 = 한 group_id.** 여러 항목이 함께 바뀌면 행을 나누고 같은
   그룹으로 묶는다. 콘솔은 그룹 단위로 카드를 그린다.
4. **변화 없으면 기록하지 않는다.** before == after 인 행은 소음이다.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendanceCorrection

# ── 센티널 ────────────────────────────────────────────────────────────────
# "값이 비어 있었다"를 명시하는 값. 콘솔은 각각 "Not set" / "(empty)" 로 표시한다.
NONE = "(none)"
EMPTY = "(empty)"

# ── target_type ──────────────────────────────────────────────────────────
TARGET_ATTENDANCE = "attendance"
TARGET_BREAK = "break"

# ── action (카드 태그) ────────────────────────────────────────────────────
ACTION_CLOCK_IN = "clock_in"
ACTION_CLOCK_OUT = "clock_out"
ACTION_BREAK_START = "break_start"
ACTION_BREAK_END = "break_end"
ACTION_NO_SHOW = "no_show"
ACTION_CANCEL = "cancel"
ACTION_REOPEN = "reopen"
ACTION_AUTO_CLOCK_OUT = "auto_clock_out"
ACTION_MODIFY = "modify"
ACTION_BREAK_ADDED = "break_added"
ACTION_BREAK_UPDATED = "break_updated"
ACTION_BREAK_REMOVED = "break_removed"
ACTION_CLEAR_TIMES = "clear_times"

# ── field_name (줄 항목) ──────────────────────────────────────────────────
FIELD_STATUS = "status"
FIELD_CLOCK_IN = "clock_in"
FIELD_CLOCK_OUT = "clock_out"
FIELD_NOTE = "note"
FIELD_BREAK_START_AT = "break_start_at"
FIELD_BREAK_END_AT = "break_end_at"
FIELD_BREAK_TYPE = "break_type"

NO_REASON = "(no reason)"


def new_group() -> uuid.UUID:
    """한 사용자 액션을 대표하는 그룹 식별자."""
    return uuid.uuid4()


def dt_value(value: datetime | None) -> str:
    """datetime → 저장 문자열. 비어 있으면 NONE 센티널."""
    return value.isoformat() if value is not None else NONE


def text_value(value: str | None) -> str:
    """자유 텍스트(note 등) → 저장 문자열. 비어 있으면 EMPTY 센티널."""
    if value is None:
        return EMPTY
    stripped = value.strip()
    return stripped if stripped else EMPTY


def plain_value(value: str | None) -> str:
    """status / break_type 처럼 코드값인 필드 → 저장 문자열."""
    return value if value else NONE


def record(
    db: AsyncSession,
    *,
    attendance_id: uuid.UUID,
    group_id: uuid.UUID,
    action: str,
    field_name: str,
    before: str,
    after: str,
    reason: str | None,
    by_user_id: uuid.UUID | None,
    target_type: str = TARGET_ATTENDANCE,
    target_id: uuid.UUID | None = None,
) -> bool:
    """타임라인 행 1개 추가. 변화가 없으면 아무것도 하지 않고 False 를 반환.

    before/after 는 이미 문자열로 정규화된 값이어야 한다 (dt_value/text_value/
    plain_value 사용). None 을 넘기지 말 것 — 센티널이 있는 이유다.
    """
    if before == after:
        return False
    db.add(
        AttendanceCorrection(
            attendance_id=attendance_id,
            group_id=group_id,
            action=action,
            field_name=field_name,
            target_type=target_type,
            target_id=target_id,
            original_value=before,
            corrected_value=after,
            reason=(reason or "").strip() or NO_REASON,
            corrected_by=by_user_id,
        )
    )
    return True


def record_status(
    db: AsyncSession,
    *,
    attendance_id: uuid.UUID,
    group_id: uuid.UUID,
    action: str,
    before: str | None,
    after: str | None,
    reason: str | None,
    by_user_id: uuid.UUID | None,
) -> bool:
    """status 전이 행. 모든 액션이 이걸 함께 남겨서 "이전 상태"가 늘 보이게 한다."""
    return record(
        db,
        attendance_id=attendance_id,
        group_id=group_id,
        action=action,
        field_name=FIELD_STATUS,
        before=plain_value(before),
        after=plain_value(after),
        reason=reason,
        by_user_id=by_user_id,
    )


def record_break_snapshot(
    db: AsyncSession,
    *,
    attendance_id: uuid.UUID,
    group_id: uuid.UUID,
    action: str,
    break_id: uuid.UUID,
    before: tuple[datetime | None, datetime | None, str | None],
    after: tuple[datetime | None, datetime | None, str | None],
    reason: str | None,
    by_user_id: uuid.UUID | None,
) -> None:
    """휴식 세션 전이 — (시작, 종료, 타입) 세 항목을 각각 한 행으로.

    추가는 before 가 전부 비어 있는 전이, 삭제는 after 가 전부 비어 있는 전이로
    표현된다. 별도 인코딩 없이 같은 규칙 하나로 add/update/delete 를 다 덮는다.
    """
    before_start, before_end, before_type = before
    after_start, after_end, after_type = after
    common = {
        "attendance_id": attendance_id,
        "group_id": group_id,
        "action": action,
        "reason": reason,
        "by_user_id": by_user_id,
        "target_type": TARGET_BREAK,
        "target_id": break_id,
    }
    record(
        db,
        field_name=FIELD_BREAK_START_AT,
        before=dt_value(before_start),
        after=dt_value(after_start),
        **common,
    )
    record(
        db,
        field_name=FIELD_BREAK_END_AT,
        before=dt_value(before_end),
        after=dt_value(after_end),
        **common,
    )
    record(
        db,
        field_name=FIELD_BREAK_TYPE,
        before=plain_value(before_type),
        after=plain_value(after_type),
        **common,
    )
