"""근무가능시간 서비스 — 검증 + 주간 저장(diff·upsert·이력) + 셀프 게이트.

트랜잭션(commit) 은 이 계층이 소유한다. 권한/IDOR 은 라우터가 게이트하고,
이 서비스는 (org, user) 로 스코프된 저장/조회만 수행한다.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.availability import StaffAvailability, StaffAvailabilityPreset
from app.models.user import User
from app.repositories.availability_repository import (
    availability_history_repository,
    availability_preset_repository,
    availability_repository,
)
from app.schemas.availability import (
    AvailabilityDayIn,
    AvailabilityDayOut,
    AvailabilityHistoryOut,
    AvailabilityMemberOut,
    MyAvailabilityOut,
    PresetCreate,
    PresetOut,
    fmt_hhmm,
    parse_hhmm,
)
from app.utils.exceptions import BadRequestError, DuplicateError, ForbiddenError, NotFoundError

DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


# ── 빌트인 SYSTEM 프리셋 (DB 행 아님, 코드 상수) ──────────────
# spec: 요일 인덱스 0=Sun..6=Sat. None=off, "full"=full, (start,end)=range.
_FULL = "full"
_AM = ("09:00", "14:30")
_DAY = ("09:00", "17:00")


def _spec_to_days(spec: list) -> list[AvailabilityDayOut]:
    days: list[AvailabilityDayOut] = []
    for dow in range(7):
        cell = spec[dow]
        if cell is None:
            days.append(AvailabilityDayOut(day_of_week=dow, state="off"))
        elif cell == _FULL:
            days.append(AvailabilityDayOut(day_of_week=dow, state="full"))
        else:
            days.append(
                AvailabilityDayOut(
                    day_of_week=dow, state="range", start_time=cell[0], end_time=cell[1]
                )
            )
    return days


# id 는 접두사 "sys-" 로 커스텀(UUID)과 구분 → delete 시 시스템 차단에 사용.
_SYSTEM_PRESET_SPECS: list[tuple[str, str, list]] = [
    ("sys-weekday-full", "Weekdays — Full", [None, _FULL, _FULL, _FULL, _FULL, _FULL, None]),
    ("sys-weekday-9-5", "Weekdays 9–5", [None, _DAY, _DAY, _DAY, _DAY, _DAY, None]),
    ("sys-weekday-am", "Weekday mornings", [None, _AM, _AM, _AM, _AM, _AM, None]),
    ("sys-weekend", "Weekends — Full", [_FULL, None, None, None, None, None, _FULL]),
    ("sys-full", "Full week", [_FULL, _FULL, _FULL, _FULL, _FULL, _FULL, _FULL]),
]

SYSTEM_PRESETS: list[PresetOut] = [
    PresetOut(id=pid, name=name, days=_spec_to_days(spec), is_system=True)
    for pid, name, spec in _SYSTEM_PRESET_SPECS
]
_SYSTEM_PRESET_IDS = {p.id for p in SYSTEM_PRESETS}


def _row_snapshot(row: StaffAvailability | None) -> dict:
    if row is None:
        return {"state": "off"}
    return {"state": row.state, "start": fmt_hhmm(row.start_time), "end": fmt_hhmm(row.end_time)}


def _in_snapshot(day: AvailabilityDayIn) -> dict:
    return {"state": day.state, "start": day.start_time, "end": day.end_time}


def _label(snap: dict) -> str:
    if snap["state"] == "off":
        return "Off"
    if snap["state"] == "full":
        return "Full"
    return f"{snap.get('start')}–{snap.get('end')}"


def _describe(dow: int, prev: dict, new: dict) -> str:
    # "Fri: Full → Off" (요일: 이전 → 이후)
    return f"{DAY_NAMES[dow]}: {_label(prev)} → {_label(new)}"


class AvailabilityService:
    # ── 조회 ────────────────────────────────────────────
    def _member(
        self,
        user_id: uuid.UUID,
        rows: list[StaffAvailability],
        full_name: str | None = None,
    ) -> AvailabilityMemberOut:
        by_dow = {r.day_of_week: r for r in rows}
        days: list[AvailabilityDayOut] = []
        updated_at: datetime | None = None
        for dow in range(7):
            r = by_dow.get(dow)
            if r is None:
                days.append(AvailabilityDayOut(day_of_week=dow, state="off"))
            else:
                days.append(
                    AvailabilityDayOut(
                        day_of_week=dow,
                        state=r.state,
                        start_time=fmt_hhmm(r.start_time),
                        end_time=fmt_hhmm(r.end_time),
                    )
                )
                if r.updated_at and (updated_at is None or r.updated_at > updated_at):
                    updated_at = r.updated_at
        return AvailabilityMemberOut(
            user_id=str(user_id), full_name=full_name, days=days, updated_at=updated_at
        )

    async def get_member(
        self, db: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID,
        full_name: str | None = None,
    ) -> AvailabilityMemberOut:
        rows = await availability_repository.list_for_user(db, organization_id, user_id)
        return self._member(user_id, rows, full_name)

    async def get_bulk(
        self, db: AsyncSession, organization_id: uuid.UUID, user_ids: list[uuid.UUID]
    ) -> list[AvailabilityMemberOut]:
        rows = await availability_repository.list_for_users(db, organization_id, user_ids)
        by_user: dict[uuid.UUID, list[StaffAvailability]] = {uid: [] for uid in user_ids}
        for r in rows:
            by_user.setdefault(r.user_id, []).append(r)
        return [self._member(uid, by_user.get(uid, [])) for uid in user_ids]

    async def get_history(
        self, db: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[AvailabilityHistoryOut]:
        rows = await availability_history_repository.list_for_user(db, organization_id, user_id)
        # 변경자 이름 batch 조회 (actor_id -> full_name)
        actor_ids = {r.actor_id for r in rows if r.actor_id}
        names: dict[uuid.UUID, str] = {}
        if actor_ids:
            from sqlalchemy import select as _select
            res = await db.execute(_select(User.id, User.full_name).where(User.id.in_(actor_ids)))
            names = {uid: fn for uid, fn in res.all()}
        return [
            AvailabilityHistoryOut(
                day_of_week=r.day_of_week,
                source=r.source,
                snapshot=r.snapshot,
                prev=r.prev,
                description=r.description,
                actor_id=str(r.actor_id) if r.actor_id else None,
                actor_name=names.get(r.actor_id) if r.actor_id else None,
                created_at=r.created_at,
            )
            for r in rows
        ]

    # ── 저장 (주간 diff + 이력) ──────────────────────────
    async def save_week(
        self,
        db: AsyncSession,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        days: list[AvailabilityDayIn],
        *,
        actor_id: uuid.UUID | None,
        source: str,
    ) -> AvailabilityMemberOut:
        current = {
            r.day_of_week: r
            for r in await availability_repository.list_for_user(db, organization_id, user_id)
        }
        # off/미포함 요일은 행 없음으로 취급
        desired = {d.day_of_week: d for d in days if d.state != "off"}

        # 한 번의 save 에서 생성되는 이력 행은 동일 타임스탬프 → 콘솔이 하나의 그룹으로 묶는다
        batch_ts = datetime.now(timezone.utc)

        for dow in range(7):
            des = desired.get(dow)
            cur = current.get(dow)
            prev_snap = _row_snapshot(cur)

            if des is None:
                if cur is not None:
                    await availability_repository.delete_row(db, cur)
                    await availability_history_repository.append(
                        db, user_id=user_id, organization_id=organization_id, day_of_week=dow,
                        actor_id=actor_id, source=source, snapshot={"state": "off"},
                        prev=prev_snap, description=_describe(dow, prev_snap, {"state": "off"}),
                        created_at=batch_ts,
                    )
                continue

            new_snap = _in_snapshot(des)
            if cur is None:
                await availability_repository.create(
                    db,
                    {
                        "user_id": user_id,
                        "organization_id": organization_id,
                        "day_of_week": dow,
                        "state": des.state,
                        "start_time": parse_hhmm(des.start_time),
                        "end_time": parse_hhmm(des.end_time),
                        "source": source,
                        "updated_by": actor_id,
                    },
                )
                await availability_history_repository.append(
                    db, user_id=user_id, organization_id=organization_id, day_of_week=dow,
                    actor_id=actor_id, source=source, snapshot=new_snap,
                    prev=prev_snap, description=_describe(dow, prev_snap, new_snap),
                    created_at=batch_ts,
                )
            elif prev_snap != new_snap:
                cur.state = des.state
                cur.start_time = parse_hhmm(des.start_time)
                cur.end_time = parse_hhmm(des.end_time)
                cur.source = source
                cur.updated_by = actor_id
                await db.flush()
                await availability_history_repository.append(
                    db, user_id=user_id, organization_id=organization_id, day_of_week=dow,
                    actor_id=actor_id, source=source, snapshot=new_snap,
                    prev=prev_snap, description=_describe(dow, prev_snap, new_snap),
                    created_at=batch_ts,
                )

        await db.commit()
        return await self.get_member(db, organization_id, user_id)

    # ── 앱/셀프 ──────────────────────────────────────────
    async def get_mine(self, db: AsyncSession, current_user: User) -> MyAvailabilityOut:
        member = await self.get_member(db, current_user.organization_id, current_user.id)
        # 당분간 정책: 최초 1회(미설정)만 본인 편집 가능.
        # history(append-only) 존재 = "한번이라도 설정됨" 판정. 행 삭제(주 전체 Off)로도 열리지 않음.
        # updated_at 은 그대로 행에서 파생(앱의 isSet/읽기전용 표시에 사용) — can_edit 게이트와 별개.
        ever_set = await availability_history_repository.exists_for_user(
            db, current_user.organization_id, current_user.id
        )
        can_edit = not ever_set
        return MyAvailabilityOut(days=member.days, can_edit=can_edit, updated_at=member.updated_at)

    async def update_mine(
        self, db: AsyncSession, current_user: User, days: list[AvailabilityDayIn]
    ) -> MyAvailabilityOut:
        # 당분간 정책: 최초 1회(미설정)만 본인 편집 가능. 설정 후에는 매니저만 변경.
        # history(append-only) 존재 = "한번이라도 설정됨" 판정. 행 삭제(주 전체 Off)로도 열리지 않음.
        if await availability_history_repository.exists_for_user(
            db, current_user.organization_id, current_user.id
        ):
            raise ForbiddenError(
                "Your availability is already set. Contact your manager or supervisor to change it."
            )
        await self.save_week(
            db, current_user.organization_id, current_user.id, days,
            actor_id=current_user.id, source="staff_self",
        )
        return await self.get_mine(db, current_user)

    # ── 프리셋 (system 상수 + org custom) ────────────────────
    def _preset_out(self, row: StaffAvailabilityPreset) -> PresetOut:
        days = [AvailabilityDayOut(**d) for d in row.days]
        return PresetOut(id=str(row.id), name=row.name, days=days, is_system=False)

    def _normalize_days(self, days: list[AvailabilityDayIn]) -> list[dict]:
        """입력 요일을 7일 스냅샷(off 채움)으로 정규화 → JSONB 저장용 dict 리스트."""
        by_dow = {d.day_of_week: d for d in days if d.state != "off"}
        out: list[dict] = []
        for dow in range(7):
            d = by_dow.get(dow)
            if d is None:
                out.append({"day_of_week": dow, "state": "off",
                            "start_time": None, "end_time": None})
            else:
                out.append({"day_of_week": dow, "state": d.state,
                            "start_time": d.start_time, "end_time": d.end_time})
        return out

    async def list_presets(
        self, db: AsyncSession, organization_id: uuid.UUID
    ) -> list[PresetOut]:
        rows = await availability_preset_repository.list_for_org(db, organization_id)
        return [*SYSTEM_PRESETS, *(self._preset_out(r) for r in rows)]

    async def create_preset(
        self,
        db: AsyncSession,
        organization_id: uuid.UUID,
        data: PresetCreate,
        *,
        actor_id: uuid.UUID | None,
    ) -> PresetOut:
        existing = await availability_preset_repository.get_by_name(
            db, organization_id, data.name
        )
        if existing is not None:
            raise DuplicateError(f"A preset named '{data.name}' already exists")
        row = await availability_preset_repository.create(
            db,
            {
                "organization_id": organization_id,
                "name": data.name,
                "days": self._normalize_days(data.days),
                "is_system": False,
                "created_by": actor_id,
            },
        )
        await db.commit()
        await db.refresh(row)
        return self._preset_out(row)

    async def delete_preset(
        self, db: AsyncSession, organization_id: uuid.UUID, preset_id: str
    ) -> None:
        # 빌트인 system 프리셋은 삭제 불가.
        if preset_id in _SYSTEM_PRESET_IDS:
            raise BadRequestError("System presets cannot be deleted")
        try:
            pid = uuid.UUID(preset_id)
        except ValueError:
            raise NotFoundError("Preset not found")
        row = await availability_preset_repository.get_owned(db, organization_id, pid)
        if row is None:
            raise NotFoundError("Preset not found")
        await availability_preset_repository.delete_row(db, row)
        await db.commit()


availability_service = AvailabilityService()
