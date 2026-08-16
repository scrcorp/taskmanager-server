"""이슈 리포트 커스텀 필드 — 해석 / 검증 / 스냅샷 / 렌더.

여기가 서버측 단일 지점이다. 라우터·서비스는 이 함수들만 부른다.
계약 SoT: `docs/99_inbox/2026-08-15-이슈리포트-description-블록화-검토.md`

핵심 규칙 세 가지:

1. **표시 대상 = 전역 `custom_fields` + 선택된 카테고리의 `fields`.**
   전역은 카테고리 무관하게 항상 뜬다. 카테고리 필드는 그 카테고리에서만.

2. **표시 대상 필드는 전부 키를 만든다. 값이 없으면 `null`.**
   그래야 "물어봤는데 안 답함"(null)과 "안 물어봄"(키 없음)이 갈린다.
   예전엔 description 한 덩어리라 둘 다 구분이 안 됐다.

3. **`fields_snapshot` 은 서버가 만든다.** 클라가 보낸 값은 무시한다.
   템플릿은 나중에 바뀌므로, 스냅샷이 없으면 과거 리포트의 null 이
   "미응답"인지 "그땐 없던 필드"인지 다시 모호해진다.
   선례: work_assignments.checklist_snapshot.
"""

import logging
from typing import Any

from app.core.error_codes.reports import (
    ISSUE_FIELD_REQUIRED,
    ISSUE_FIELD_VALUE_INVALID,
    ISSUE_FIELD_VALUES_MALFORMED,
)
from app.schemas.report import ISSUE_FIELD_TYPES

logger = logging.getLogger(__name__)

# 스냅샷에 남길 키. 표시·해석에 필요한 것만 (sort_order 는 순서 확정 후엔 무의미).
_SNAPSHOT_KEYS = (
    "id", "type", "label", "required", "placeholder", "helper_text",
    "options", "max_length", "min", "max", "decimals",
)


def _as_list(v: Any) -> list[dict]:
    return [f for f in v if isinstance(f, dict)] if isinstance(v, list) else []


def resolve_issue_fields(
    template_payload: dict | None, category_code: str | None
) -> list[dict]:
    """이 카테고리에서 실제로 보여줄 필드를 순서대로 돌려준다.

    순서 = `field_order` 기준. 목록에 없는 필드는 뒤에 `sort_order` 순으로 붙는다
    (템플릿에 필드를 추가했는데 field_order 갱신을 깜빡해도 필드가 사라지지 않게).
    표준 필드 자리표시자(`__title` 등)는 커스텀 필드가 아니므로 여기서 제외한다.
    """
    tpl = template_payload or {}
    fields: list[dict] = list(_as_list(tpl.get("custom_fields")))

    if category_code:
        for c in _as_list(tpl.get("categories")):
            if c.get("code") == category_code:
                fields.extend(_as_list(c.get("fields")))
                break

    # id 중복 제거 — 전역과 카테고리에 같은 id 가 있으면 카테고리 쪽이 이긴다.
    by_id: dict[str, dict] = {}
    for f in fields:
        fid = f.get("id")
        if isinstance(fid, str) and fid:
            by_id[fid] = f

    order = tpl.get("field_order")
    order = [k for k in order if isinstance(k, str)] if isinstance(order, list) else []

    out: list[dict] = []
    seen: set[str] = set()
    for key in order:
        if key.startswith("__"):
            continue  # 표준 필드 자리표시자
        f = by_id.get(key)
        if f is not None and key not in seen:
            out.append(f)
            seen.add(key)

    rest = [f for fid, f in by_id.items() if fid not in seen]
    rest.sort(key=lambda f: (f.get("sort_order") or 0))
    out.extend(rest)
    return out


def build_fields_snapshot(fields: list[dict]) -> list[dict]:
    """리포트에 남길 "그때 물어본 정의". 표시에 필요한 키만 추린다."""
    snap: list[dict] = []
    for f in fields:
        item = {k: f[k] for k in _SNAPSHOT_KEYS if f.get(k) is not None}
        if not item.get("id"):
            continue
        item.setdefault("type", "short_text")
        item.setdefault("label", item["id"])
        snap.append(item)
    return snap


def _num_text(v: Any) -> str:
    """1.0 → "1". 힌트 문구에 소수점이 지저분하게 붙지 않게."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else str(f)


def _range_hint(field: dict) -> str:
    """범위 위반 힌트. 양쪽이 다 있으면 구간으로 말한다."""
    lo, hi = field.get("min"), field.get("max")
    if lo is not None and hi is not None:
        return f"Enter a number between {_num_text(lo)} and {_num_text(hi)}."
    if lo is not None:
        return f"Enter {_num_text(lo)} or more."
    return f"Enter {_num_text(hi)} or less."


def _check_number(field: dict, val: Any, label: str) -> float | int:
    """숫자 검증. **왜 막혔는지를 hint 로 구체화한다** — "허용되지 않는 값" 만으로는
    작성자가 무엇을 고쳐야 할지 알 수 없다."""
    try:
        num = float(val)
    except (TypeError, ValueError):
        raise ISSUE_FIELD_VALUE_INVALID(
            field=label, reason="number", hint="Enter a number."
        )

    decimals = field.get("decimals")
    decimals = 0 if decimals is None else int(decimals)
    if decimals <= 0:
        if num != int(num):
            raise ISSUE_FIELD_VALUE_INVALID(
                field=label, reason="whole_number",
                hint="Enter a whole number — no decimal point.",
            )
        num = int(num)
    else:
        num = round(num, decimals)

    lo, hi = field.get("min"), field.get("max")
    if lo is not None and num < float(lo):
        raise ISSUE_FIELD_VALUE_INVALID(
            field=label, reason="min", min=lo, max=hi, hint=_range_hint(field)
        )
    if hi is not None and num > float(hi):
        raise ISSUE_FIELD_VALUE_INVALID(
            field=label, reason="max", min=lo, max=hi, hint=_range_hint(field)
        )
    return num


def _is_blank(v: Any) -> bool:
    return v is None or v == "" or v == []


def validate_and_normalize_values(
    fields: list[dict], raw_values: Any
) -> dict[str, Any]:
    """제출값을 검증하고 저장형으로 정규화한다.

    - 표시 대상 필드는 **전부 키를 만든다**. 미응답은 `None`(=JSON null)
    - `required` 인데 비었으면 400
    - 표시 대상이 아닌 키는 **보존한다** (구버전 클라 잔재를 조용히 지우지 않는다).
      단 검증하지 않는다 — 정의가 없으므로 검증할 근거가 없다.
    """
    if raw_values is None:
        raw_values = {}
    if not isinstance(raw_values, dict):
        raise ISSUE_FIELD_VALUES_MALFORMED()

    out: dict[str, Any] = {}
    known: set[str] = set()

    for f in fields:
        fid = f.get("id")
        if not isinstance(fid, str) or not fid:
            continue
        known.add(fid)
        label = f.get("label") or fid
        ftype = f.get("type") or "short_text"
        val = raw_values.get(fid)

        if ftype not in ISSUE_FIELD_TYPES:
            # 폐기된 타입(checkbox)이나 오타가 템플릿에 남아 있어도 **작성을 막지 않는다.**
            # 여기서 400 을 내면 템플릿 설정 실수 하나로 그 매장 이슈 등록이 전부 멈춘다.
            # 값은 보존하고 required 만 지킨다.
            logger.warning(
                "issue field '%s' has unsupported type %r — validation skipped", fid, ftype
            )
            if f.get("required") and _is_blank(val):
                raise ISSUE_FIELD_REQUIRED(field=label)
            out[fid] = None if _is_blank(val) else val
            continue

        if _is_blank(val):
            if f.get("required"):
                raise ISSUE_FIELD_REQUIRED(field=label)
            out[fid] = None  # 물어봤으나 미응답 — 명시 기록
            continue

        if ftype == "number":
            out[fid] = _check_number(f, val, label)
        elif ftype == "single_choice":
            opts = f.get("options") or []
            if val not in opts:
                raise ISSUE_FIELD_VALUE_INVALID(
                    field=label, reason="options", options=opts,
                    hint=f"Choose one of: {', '.join(str(o) for o in opts)}.",
                )
            out[fid] = val
        elif ftype == "multi_choice":
            opts = f.get("options") or []
            if not isinstance(val, list) or any(v not in opts for v in val):
                raise ISSUE_FIELD_VALUE_INVALID(
                    field=label, reason="options", options=opts,
                    hint=f"Choose from: {', '.join(str(o) for o in opts)}.",
                )
            out[fid] = val
        else:  # short_text / long_text
            if not isinstance(val, str):
                raise ISSUE_FIELD_VALUE_INVALID(
                    field=label, reason="text", hint="Enter text."
                )
            max_len = f.get("max_length")
            if max_len and len(val) > int(max_len):
                raise ISSUE_FIELD_VALUE_INVALID(
                    field=label, reason="max_length", max_length=int(max_len),
                    hint=f"Use at most {int(max_len)} characters.",
                )
            out[fid] = val

    # 정의 밖 키 보존
    for k, v in raw_values.items():
        if k not in known:
            out[k] = v
    return out


def render_field_values_text(fields: list[dict], values: dict | None) -> str:
    """필드 값을 사람이 읽는 평문으로. Task promote 처럼 평문 컬럼에 넣을 때만 쓴다.

    **`payload.description` 을 이 값으로 덮어쓰지 않는다** — description 은 자유 서술
    칸이라는 성격을 유지한다(덮어쓰면 사용자가 쓴 문장이 사라진다).
    미응답(None)은 줄 자체를 만들지 않는다.
    """
    values = values or {}
    lines: list[str] = []
    for f in fields:
        fid = f.get("id")
        if not fid or fid not in values:
            continue
        v = values[fid]
        if v is None or v == "" or v == []:
            continue
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        lines.append(f"{f.get('label') or fid}: {v}")
    return "\n".join(lines)
