"""EMPID 채번 커서 — API 층 어댑터.

계약 SoT: `docs/99_inbox/2026-08-18 empid 채번 API계약·규칙.md` §3-1~3-3 / §4.

**판정과 계산은 여기 없다.** RULE-A(발급)·B(편입 승격)·C(재계산)·E(불일치)는 전부
`app/services/org_numbering.py` 단일 게이트웨이가 갖는다(INV-7). 이 모듈이 하는 일은
세 가지뿐이다.

1. 게이트웨이의 `EmpidCursorState` → 계약 §3-1 의 `numbering` 응답 객체로 변환
2. 입력 검증(사유 필수·커서 값 범위·번호대 문맥) → 계약 §4 의 에러 코드로 거절
3. 트랜잭션 경계(commit) 관리

콘솔은 여기서 나간 numbering 객체를 **표시만** 한다 — 다음 번호도, 예외 여부도
콘솔이 계산하지 않는다(INV-8).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes.empid import (
    ERR_CURSOR_INVALID,
    ERR_RANGE_IGNORED,
    ERR_REASON_REQUIRED,
)
from app.models.organization import NUMBERING_MODE_GROUP, StoreGroup
from app.schemas.organization import NumberingInfo
from app.services import org_numbering

# 커서 보유 주체 — 게이트웨이 상수를 그대로 재노출한다(문자열을 두 곳에 두지 않는다).
SCOPE_GROUP = org_numbering.EMPID_SCOPE_GROUP
SCOPE_STORE = org_numbering.EMPID_SCOPE_STORE

# 사유 최대 길이 — empid_changes.reason 컬럼과 동일
REASON_MAX_LENGTH = 500


def _to_info(state: org_numbering.EmpidCursorState) -> NumberingInfo:
    """게이트웨이 상태 → 계약 §3-1 numbering 객체."""
    return NumberingInfo(**state.as_dict())


# ---------------------------------------------------------------------------
# 조회 (§3-1)
# ---------------------------------------------------------------------------


async def numbering_for_stores(
    db: AsyncSession, store_ids: list[UUID]
) -> dict[UUID, NumberingInfo]:
    """매장 id → numbering 객체 (목록/상세 응답에 얹는다).

    Shared 그룹 소속 매장들은 **커서를 공유**하므로 스코프 단위로 한 번만 계산한다
    (같은 그룹 매장이 10개여도 상태 조회는 1회).
    """
    out: dict[UUID, NumberingInfo] = {}
    cache: dict[tuple[str, UUID], NumberingInfo] = {}
    for store_id in store_ids:
        scope = await org_numbering.empid_cursor_scope(db, store_id)
        key = (scope.scope, scope.scope_id)
        if key not in cache:
            state = await org_numbering.empid_cursor_state(
                db,
                group_id=scope.scope_id if scope.is_group else None,
                store_id=None if scope.is_group else store_id,
            )
            cache[key] = _to_info(state)
        out[store_id] = cache[key]
    return out


async def numbering_for_store(
    db: AsyncSession, store_id: UUID
) -> NumberingInfo | None:
    """매장 하나의 numbering 객체 (상세/생성/수정 응답용)."""
    return (await numbering_for_stores(db, [store_id])).get(store_id)


async def numbering_for_groups(
    db: AsyncSession, group_ids: list[UUID]
) -> dict[UUID, NumberingInfo]:
    """그룹 id → numbering 객체 (Groups 패널이 커서·재계산 UI 를 그린다)."""
    out: dict[UUID, NumberingInfo] = {}
    for group_id in group_ids:
        state = await org_numbering.empid_cursor_state(db, group_id=group_id)
        out[group_id] = _to_info(state)
    return out


async def numbering_for_group(
    db: AsyncSession, group_id: UUID
) -> NumberingInfo | None:
    """그룹 하나의 numbering 객체."""
    return (await numbering_for_groups(db, [group_id])).get(group_id)


# ---------------------------------------------------------------------------
# 번호대 문맥 거절 (§4 ERR-RANGE-IGNORED)
# ---------------------------------------------------------------------------


async def assert_range_start_allowed(
    db: AsyncSession, organization_id: UUID, group_id: UUID | None
) -> None:
    """Shared 그룹(numbering_mode="group")에 속할 매장의 number_range_start 는 거절한다.

    예전에는 조용히 저장하고 채번에서 무시했다 — 저장은 됐는데 아무 일도 일어나지
    않는 조용한 실패라, 운영자는 번호대를 바꿨다고 믿는다(§4 는 "거절"을 택했다).
    """
    if group_id is None:
        return
    mode = await db.scalar(
        select(StoreGroup.numbering_mode).where(
            StoreGroup.id == group_id, StoreGroup.organization_id == organization_id
        )
    )
    if mode == NUMBERING_MODE_GROUP:
        raise ERR_RANGE_IGNORED()


# ---------------------------------------------------------------------------
# 수동 조정 (§3-2) · 재계산 (§3-3)
# ---------------------------------------------------------------------------


def _clean_reason(reason: str | None) -> str:
    """사유 필수 검증 — 공백만 있는 것도 누락으로 본다."""
    text = (reason or "").strip()
    if not text:
        raise ERR_REASON_REQUIRED()
    return text[:REASON_MAX_LENGTH]


async def _scope_of(
    db: AsyncSession, scope: str, scope_id: UUID
) -> org_numbering.EmpidCursorScope:
    """스코프 객체를 게이트웨이에서 받아온다 (매장/그룹 진입 경로 통일)."""
    if scope == SCOPE_GROUP:
        return await org_numbering.group_cursor_scope(db, scope_id)
    return await org_numbering.empid_cursor_scope(db, scope_id)


async def set_cursor(
    db: AsyncSession,
    *,
    scope: str,
    scope_id: UUID,
    next_empid: int,
    reason: str | None,
    actor_id: UUID | None,
) -> tuple[NumberingInfo, int | None, bool]:
    """커서 수동 조정 (§3-2) → (numbering, previous, lowered).

    낮추는 것도 허용한다 — INV-2(전진만)의 유일한 예외가 운영자의 명시 조작이다.
    대신 사유를 강제하고 이력(source='cursor')에 남기며, 응답의 lowered 로 콘솔이
    확인 UI 를 띄운다.
    """
    # bool 은 int 의 서브클래스라 True 가 1 로 통과한다 — 명시적으로 막는다.
    if isinstance(next_empid, bool) or not isinstance(next_empid, int) or next_empid < 1:
        raise ERR_CURSOR_INVALID()
    clean_reason = _clean_reason(reason)

    cursor_scope = await _scope_of(db, scope, scope_id)
    try:
        previous = await org_numbering.set_empid_cursor(
            db,
            scope=cursor_scope,
            value=next_empid,
            reason=clean_reason,
            changed_by=actor_id,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    lowered = previous is not None and next_empid < previous
    state = await org_numbering.empid_cursor_state(
        db,
        group_id=scope_id if scope == SCOPE_GROUP else None,
        store_id=None if scope == SCOPE_GROUP else scope_id,
    )
    return _to_info(state), previous, lowered


async def recalculate_cursor(
    db: AsyncSession,
    *,
    scope: str,
    scope_id: UUID,
    apply: bool,
    reason: str | None,
    actor_id: UUID | None,
) -> tuple[NumberingInfo, bool, int | None]:
    """커서 재계산 (§3-3) → (numbering, applied, previous).

    apply=false 면 아무것도 쓰지 않는 미리보기다. apply=true 면 사유가 필수이며,
    재계산값이 현재 커서보다 낮아도 적용한다(운영자 명시 조작 — INV-2 예외).
    계산 본체는 게이트웨이의 RULE-C 다.
    """
    clean_reason = _clean_reason(reason) if apply else None

    kwargs = (
        {"group_id": scope_id} if scope == SCOPE_GROUP else {"store_id": scope_id}
    )
    try:
        state, previous = await org_numbering.recalculate_empid_cursor(
            db, apply=apply, reason=clean_reason, changed_by=actor_id, **kwargs
        )
        if apply:
            await db.commit()
    except Exception:
        await db.rollback()
        raise
    return _to_info(state), apply, previous


# ---------------------------------------------------------------------------
# 커서 초기화 — 신규 매장/그룹
# ---------------------------------------------------------------------------


def initial_cursor(
    number_range_start: int | None, group_range_start: int | None = None
) -> int:
    """신규 매장/그룹의 커서 시작값 = floor (매장값 > 그룹값 > 1).

    백필(마이그레이션)이 기존 행을 전부 채웠으므로 새로 만드는 행도 여기서 채운다.
    게이트웨이는 커서가 NULL 이면 발급 시 floor 로 지연 초기화하지만, 그러면 조회
    응답의 `numbering.next_empid` 가 null 이라 콘솔의 "New EMPIDs: 7044, 7045…"
    프리뷰가 첫 발급 전까지 아무것도 못 그린다.
    """
    return number_range_start or group_range_start or 1


async def group_range_start(db: AsyncSession, group_id: UUID | None) -> int | None:
    """그룹 기본 번호대 시작값 (미그룹이면 None)."""
    if group_id is None:
        return None
    return await db.scalar(
        select(StoreGroup.number_range_start).where(StoreGroup.id == group_id)
    )


# ---------------------------------------------------------------------------
# RULE-B 편입 승격 — 게이트웨이 호출 배선
# ---------------------------------------------------------------------------


async def promote_group_cursor_on_join(
    db: AsyncSession,
    store_id: UUID,
    group_id: UUID,
    *,
    actor_id: UUID | None = None,
) -> None:
    """매장이 그룹에 편입될 때 그룹 커서를 승격시킨다 (RULE-B).

        group.next_empid = max(group.next_empid, store.next_empid)

    **커서끼리** 비교한다 — MAX(empid)+1 로 하면 그 매장의 예외 번호가 그룹 커서를
    밀어올린다(INV-1). 계산 본체는 게이트웨이가 갖고, 여기서는 편성 변경 지점에
    배선만 한다(계약 RULE-B 주석이 지목한 store_service 담당분).
    """
    await org_numbering.promote_group_cursor(
        db,
        group_id=group_id,
        store_id=store_id,
        changed_by=actor_id,
        reason="store joined group",
    )
