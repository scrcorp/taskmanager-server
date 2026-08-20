"""배정 가능 여부 — "이 사람에게 이 날짜로 근무를 꽂아도 되는가".

**판정은 여기 한 곳에서만 한다.** 스케줄 저장 검증(`schedule_service._validate_entry`)과
화면이 칸을 잠그기 위해 읽는 API(`roster` / `users`)가 같은 함수를 쓴다. 둘이 갈리면
"화면에선 눌리는데 저장이 안 되는" 상태가 되고, 그게 이 기능이 생긴 원인이었다.

판정 축은 **고용 상태 하나**다. 매장 배정 여부는 스케줄 검증이 이미 따로 본다
(`USER_NOT_IN_STORE` / `USER_NOT_MARKED_FOR_STORE`) — 여기서 중복해서 보지 않는다.

규칙 (D1 · 2026-08-19):
  - 미가입(유령) / 활성    → 무제한
  - 퇴사(`termination_date` 있음) → **그 날짜까지** 배정 가능. 다음날부터 차단.
    `termination_date` 는 **마지막 근무일(inclusive)** 이다 — `offboarding_service` 와
    `records_retention_service` 가 이미 그 정의로 동작한다.
  - 퇴사일 없이 비활성(일반 삭제/토글) → 판정 기준이 없으므로 **전 날짜 차단** (D1-a)
  - 이 조직 소속이 아니거나 `deleted_at` → 전 날짜 차단 (D4, org IDOR 동시 차단)

[향후] 소속의 단위는 지금 org(`org_members`)지만, 조직계층 재정의가 끝나면
group(현 `store_groups`) 이 그 자리를 가져간다. 바뀌는 것은 **이 파일 안의 조회뿐**이고,
호출부(검증·API 계약 `assignable_until`)는 그대로 살아야 한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import schedule_codes as codes
from app.models.org_member import OrgMember
from app.models.user import User


@dataclass(frozen=True)
class Assignability:
    """한 사람의 배정 가능 범위 + 그 판정의 근거가 된 재직 사실.

    employed=False           → 어떤 날짜로도 배정 불가
    employed=True, until=None → 제한 없음
    employed=True, until=D    → D 까지(당일 포함) 배정 가능

    hire_date / termination_date 는 **표시용 재직기간**이다. 판정(`assignable_until`)과
    값이 같아 보여도 역할이 다르다 — 판정은 게이트, 이쪽은 "언제부터 언제까지 일했나".
    hire_date 는 현재 대부분 비어 있다(입력 경로가 아직 없음) → 화면은 빈칸으로 둔다.
    """

    user_id: UUID
    employed: bool
    assignable_until: date | None
    hire_date: date | None = None
    termination_date: date | None = None

    def allows(self, operating_day: date) -> bool:
        if not self.employed:
            return False
        return self.assignable_until is None or operating_day <= self.assignable_until


#: 조회되지 않은 사람(타 org / 삭제 / 존재하지 않음)의 기본값 — fail-closed.
def _blocked(user_id: UUID) -> Assignability:
    return Assignability(user_id=user_id, employed=False, assignable_until=None)


async def get_assignability(
    db: AsyncSession,
    organization_id: UUID,
    user_ids: Sequence[UUID],
) -> dict[UUID, Assignability]:
    """여러 사람의 배정 가능 범위를 한 번에 조회한다 (N+1 금지).

    조회되지 않은 id 는 결과에 **차단으로 채워서** 돌려준다 — 호출부가 `.get()` 의
    None 을 각자 해석하다가 한쪽만 열리는 일이 없게 하기 위함이다.
    """
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return {}

    rows = await db.execute(
        select(
            User.id,
            User.is_active,
            User.is_provisional,
            OrgMember.status,
            OrgMember.termination_date,
            OrgMember.hire_date,
        )
        .outerjoin(
            OrgMember,
            (OrgMember.user_id == User.id)
            & (OrgMember.organization_id == organization_id),
        )
        .where(
            User.id.in_(ids),
            User.organization_id == organization_id,
            User.deleted_at.is_(None),
        )
    )

    out: dict[UUID, Assignability] = {uid: _blocked(uid) for uid in ids}
    for uid, is_active, is_provisional, member_status, term, hired in rows.all():
        out[uid] = _judge(uid, is_active, is_provisional, member_status, term, hired)
    return out


def _judge(
    user_id: UUID,
    is_active: bool,
    is_provisional: bool,
    member_status: str | None,
    termination_date: date | None,
    hire_date: date | None = None,
) -> Assignability:
    # 퇴사 처리된 사람은 계정이 아직 활성이어도 퇴사일이 상한이다.
    # (offboard 는 둘을 함께 바꾸지만, 어느 한쪽만 바뀐 데이터가 실재할 수 있어
    #  더 좁은 쪽을 택한다 — fail-closed.)
    if termination_date is not None and (member_status == "terminated" or not is_active):
        return Assignability(
            user_id=user_id, employed=True, assignable_until=termination_date,
            hire_date=hire_date, termination_date=termination_date,
        )
    # 유령(미가입)은 is_active=False 지만 '앞으로 일할 사람' 이라 배정 대상이 맞다.
    if is_provisional or is_active:
        return Assignability(
            user_id=user_id, employed=True, assignable_until=None,
            hire_date=hire_date, termination_date=termination_date,
        )
    return Assignability(
        user_id=user_id, employed=False, assignable_until=None,
        hire_date=hire_date, termination_date=termination_date,
    )


def blocking_issue(a: Assignability, operating_day: date) -> dict[str, Any] | None:
    """배정이 막히면 스케줄 검증 계약(`codes.issue`) 형태로, 아니면 None."""
    if a.allows(operating_day):
        return None
    if a.assignable_until is not None:
        return codes.issue(
            codes.USER_TERMINATED_BEFORE_DATE,
            user_id=str(a.user_id),
            termination_date=a.assignable_until.isoformat(),
            operating_day=operating_day.isoformat(),
        )
    return codes.issue(codes.USER_NOT_EMPLOYED, user_id=str(a.user_id))


async def assert_assignable(
    db: AsyncSession,
    organization_id: UUID,
    user_id: UUID,
    operating_day: date,
) -> dict[str, Any] | None:
    """단건 판정 — 막히면 issue dict, 통과면 None."""
    info = (await get_assignability(db, organization_id, [user_id]))[user_id]
    return blocking_issue(info, operating_day)


def blocking_message(a: Assignability, operating_day: date) -> str | None:
    """스케줄 계약(codes) 을 쓰지 않는 도메인용 — 사람이 읽는 한 문장.

    팁·연락처처럼 스케줄 에러 코드 체계를 공유하지 않는 곳에서 쓴다. 문구를 도메인마다
    새로 쓰면 같은 상황이 화면마다 다르게 설명된다.
    """
    if a.allows(operating_day):
        return None
    if a.assignable_until is not None:
        return (
            f"This employee's last working day was {a.assignable_until.isoformat()}, "
            f"so nothing can be recorded for them on {operating_day.isoformat()}."
        )
    return "This employee is no longer active, so nothing can be recorded for them."
