"""schedule time policy settings (D2)

세 축 분리(경계 / 영업시간 / 스케줄 시간대)를 설정 레이어에 반영한다.

1. `store.operating_hours` registry 행 INSERT (신설, 멱등)
2. `schedule.range` / `work.default_schedule_duration_minutes` 기존 행 UPDATE
   — **시드는 INSERT-only 라 시드 수정만으로는 절대 반영되지 않는다.**
3. 기존 저장값(org_settings / store_settings) 을 표준 형태로 정규화
   — `26:00` 같은 24 초과 표기 → `02:00` + `end_offset_days: 1` (D2-8)
4. `stores.operating_hours` 컬럼 DROP — 출처가 둘이면 다시 갈라진다

⚠️ 이 파일은 **앱 코드를 import 하지 않는다.** 마이그레이션은 그때의 스키마를 고정한
기록이라, app.utils / app.seeds 를 참조하면 나중에 그쪽이 바뀌는 순간 과거 배포가 깨진다.
표준 형태의 정의가 바뀌면 이 파일이 아니라 **새 마이그레이션**을 쓴다.

Revision ID: a7c1e9d40b52
Revises: 93e5d4c4a7fd
Create Date: 2026-08-10

"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a7c1e9d40b52'
down_revision: Union[str, None] = '93e5d4c4a7fd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# 영업시간 기본값은 **미설정**(형태만 표준, 값은 빈 칸). 그럴듯한 값을 기본으로 두면
# 아직 설정하지 않은 매장에서 야간 시프트가 영업시간 밖으로 판정돼 일일 리포트의
# 인원 부족 검사에서 통째로 빠진다. 미설정 = 제한 없음(전부 검사)이어야 한다.
_OPERATING_HOURS_DEFAULT = {"mode": "all", "all": {}, "per_day": {}, "closed": []}

_SCHEDULE_RANGE_DEFAULT = {
    "mode": "all",
    "all": {"start": "06:00", "end": "23:00", "end_offset_days": 0},
    "per_day": {},
    "closed": [],
}

_OPERATING_HOURS_LABEL = "Store Operating Hours"
_OPERATING_HOURS_DESC = (
    "Hours the store is open to customers. Staff working hours are set separately "
    "and normally extend past these (prep and closing). "
    "Days listed as closed still accept schedules, with a warning."
)

# 키 이름은 옛 것(그리드 표시 범위)이고 의미는 D2-4(직원 근무 가능 시간대)다.
# 키 문자열은 settings_registry 의 PK이고 org_settings/store_settings 가 FK로 참조하므로
# 실 데이터가 있는 키를 이름값 때문에 옮기지 않는다 (Q3). 바뀌는 건 label/description 뿐.
_SCHEDULE_RANGE_LABEL = "Staff Working Hours"
_SCHEDULE_RANGE_DESC = (
    "Hours staff can be scheduled — store operating hours plus prep and closing time. "
    "The schedule grid draws this range, and supervisor coverage gaps are measured against it. "
    "Keep it wider than the operating hours, or the difference is never checked for coverage."
)

_DURATION_LABEL = "Default shift length (minutes)"
_DURATION_DESC = (
    "Length applied to a newly created schedule when the work role has no default times. "
    "Also the length of an auto-created walk-in schedule (end time = clock-in time + this value). "
    "Console, kiosk and app all read this one value."
)


def _normalize_entry(entry):
    """`{"start","end"}` 한 칸을 표준 형태로. 24 초과 표기를 오프셋으로 편다."""
    if not isinstance(entry, dict):
        return None
    out = dict(entry)
    for field in ("start", "end"):
        raw = out.get(field)
        if not isinstance(raw, str) or ":" not in raw:
            continue
        try:
            h, m = (int(x) for x in raw.split(":", 1))
        except ValueError:
            continue
        days, h = divmod(h, 24)
        out[field] = f"{h:02d}:{m:02d}"
        if field == "end" and days:
            # 24+ 표기가 뜻하던 "다음 날"을 명시적 오프셋으로 옮긴다 (MSK `26:00` → `02:00` +1d).
            out["end_offset_days"] = int(out.get("end_offset_days") or 0) + days
    out.setdefault("end_offset_days", 0)
    return out


def _normalize_range_value(value):
    """저장된 schedule.range 값 전체를 `{mode, all, per_day, closed}` 로 맞춘다.

    옛 값은 모양이 셋이었다: `{"all":{...}}`, `{mode, all, per_day}`,
    그리고 레거시 top-level 요일 키. 클라이언트마다 보정 코드가 생긴 원인이라 여기서 하나로 만든다.
    """
    if not isinstance(value, dict):
        return None

    per_day = {}
    raw_per_day = value.get("per_day")
    if isinstance(raw_per_day, dict):
        for key, entry in raw_per_day.items():
            got = _normalize_entry(entry)
            if got is not None:
                per_day[key] = got
    # 레거시 top-level 요일 키 → per_day 로 흡수
    for key in _WEEKDAYS:
        if key in per_day:
            continue
        got = _normalize_entry(value.get(key))
        if got is not None:
            per_day[key] = got

    all_entry = _normalize_entry(value.get("all")) or dict(_SCHEDULE_RANGE_DEFAULT["all"])

    mode = value.get("mode")
    if mode not in ("all", "per_day"):
        mode = "per_day" if per_day and not isinstance(value.get("all"), dict) else "all"

    closed = value.get("closed")
    if not isinstance(closed, list):
        closed = []

    return {"mode": mode, "all": all_entry, "per_day": per_day, "closed": closed}


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1) 신규 키 INSERT ────────────────────────────────────────
    # INSERT 주체는 **이 마이그레이션**이다 (배포 시점에 확정). 시드
    # (app/seeds/settings_seed.py) 는 같은 정의를 갖고 "없으면 넣는다" 라서 멱등이며,
    # 빈 DB(로컬 최초 기동)에서는 시드가 넣는다. 두 정의를 항상 같이 고칠 것.
    conn.execute(
        sa.text(
            """
            INSERT INTO settings_registry
                (key, label, description, value_type, levels, default_priority,
                 default_value, validation_schema, category, created_at, updated_at)
            VALUES
                ('store.operating_hours', :label, :description, 'json',
                 CAST(:levels AS jsonb), 'item', CAST(:default_value AS jsonb),
                 NULL, 'Store Hours', NOW(), NOW())
            ON CONFLICT (key) DO NOTHING
            """
        ),
        {
            "label": _OPERATING_HOURS_LABEL,
            "description": _OPERATING_HOURS_DESC,
            "levels": json.dumps(["org", "store"]),
            "default_value": json.dumps(_OPERATING_HOURS_DEFAULT),
        },
    )

    # ── 2) 기존 키 UPDATE ───────────────────────────────────────
    conn.execute(
        sa.text(
            """
            UPDATE settings_registry
               SET label = :label,
                   description = :description,
                   default_value = CAST(:default_value AS jsonb),
                   category = 'Store Hours',
                   updated_at = NOW()
             WHERE key = 'schedule.range'
            """
        ),
        {
            "label": _SCHEDULE_RANGE_LABEL,
            "description": _SCHEDULE_RANGE_DESC,
            "default_value": json.dumps(_SCHEDULE_RANGE_DEFAULT),
        },
    )
    conn.execute(
        sa.text(
            """
            UPDATE settings_registry
               SET label = :label, description = :description, updated_at = NOW()
             WHERE key = 'work.default_schedule_duration_minutes'
            """
        ),
        {"label": _DURATION_LABEL, "description": _DURATION_DESC},
    )

    # ── 3) 기존 저장값 정규화 ────────────────────────────────────
    # 파서에서 "종료 < 시작이면 +1일" 암묵 보정을 없애기 때문에 **정규화가 선행이어야 한다.**
    # 안 하면 MSK 의 `03:00–26:00` 이 파싱 실패로 떨어져 SV 공백 검사가 조용히 멈춘다.
    for table, id_col in (("org_settings", "id"), ("store_settings", "id")):
        rows = conn.execute(
            sa.text(f"SELECT {id_col}, value FROM {table} WHERE key = 'schedule.range'")
        ).fetchall()
        for row_id, value in rows:
            normalized = _normalize_range_value(value)
            if normalized is None or normalized == value:
                continue
            conn.execute(
                sa.text(f"UPDATE {table} SET value = CAST(:v AS jsonb) WHERE {id_col} = :id"),
                {"v": json.dumps(normalized), "id": row_id},
            )

    # ── 4) stores.operating_hours 컬럼 DROP ─────────────────────
    # 값이 있는 매장이 하나라도 있으면 멈춘다. 배포가 실패하는 편이,
    # 아무도 모르게 운영 데이터가 사라지는 것보다 낫다.
    leftover = conn.execute(
        sa.text("SELECT count(*) FROM stores WHERE operating_hours IS NOT NULL")
    ).scalar_one()
    if leftover:
        raise RuntimeError(
            f"{leftover} store(s) still have stores.operating_hours set. "
            "Move the values into the 'store.operating_hours' setting "
            "(store_settings) before running this migration."
        )
    op.drop_column("stores", "operating_hours")


def downgrade() -> None:
    # 컬럼은 되살리되 값은 복구하지 않는다 (전 매장 NULL 이었으므로 잃은 것이 없다).
    op.add_column(
        "stores",
        sa.Column("operating_hours", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute("DELETE FROM settings_registry WHERE key = 'store.operating_hours'")
    # label/description/default_value 와 정규화된 저장값은 되돌리지 않는다 —
    # 옛 24+ 표기로 되돌리는 것은 복구가 아니라 재발이다.
