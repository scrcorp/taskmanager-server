"""Integration test — 설정 신설/정리와 영업일 경계 스냅샷 (D2).

지키는 것 둘:
  1. `store.operating_hours` / `schedule.range` registry 행이 **표준 값 형태**로 존재한다.
     시드는 INSERT-only 라 기존 키는 UPDATE 마이그레이션으로만 바뀐다 — 마이그레이션이
     빠지면 여기서 잡힌다 (시드만 고치고 배포해 아무것도 안 바뀌는 사고 방지).
  2. 매장 생성 시 조직 기본 경계가 **복사(스냅샷)** 된다. 라이브 cascade 가 아니다 —
     조직 기본값을 나중에 바꿔도 기존 매장의 경계는 움직이지 않아야 한다.

⚠️ 이 모듈은 organizations.day_start_time 과 매장을 만들었다 지운다.
경계 값이 남으면 뒤따르는 테스트의 work_date 판정이 통째로 바뀌므로 **반드시 원복**한다.
"""

from __future__ import annotations

from datetime import time
from uuid import UUID

import pytest
from sqlalchemy import delete, select

from app.database import async_session
from app.models.organization import Organization, Store
from app.models.settings import SettingsRegistry
from app.schemas.organization import StoreCreate
from app.seeds.settings_seed import SCHEDULE_RANGE_KEY, STORE_OPERATING_HOURS_KEY
from app.services.store_service import StoreService
from app.utils.timezone import resolve_day_range

pytestmark = pytest.mark.asyncio

_STANDARD_KEYS = ("mode", "all", "per_day", "closed")


class TestRegistryValueShape:
    @pytest.mark.parametrize("key", [STORE_OPERATING_HOURS_KEY, SCHEDULE_RANGE_KEY])
    async def test_registered_with_standard_shape(self, key: str):
        async with async_session() as db:
            row = await db.scalar(select(SettingsRegistry).where(SettingsRegistry.key == key))
        assert row is not None, f"{key} registry row missing — migration not applied?"
        assert row.value_type == "json"
        assert row.levels == ["org", "store"]
        assert row.default_priority == "item"
        for field in _STANDARD_KEYS:
            assert field in row.default_value, f"{key} default_value missing '{field}'"

    async def test_schedule_range_default_parses(self):
        # 기본값도 파서를 통과해야 한다 — 기본값만 다른 모양이던 것이 보정 코드의 출발점이었다.
        async with async_session() as db:
            row = await db.scalar(
                select(SettingsRegistry).where(SettingsRegistry.key == SCHEDULE_RANGE_KEY)
            )
        assert resolve_day_range(row.default_value, 0) == (360, 1380)

    async def test_operating_hours_default_is_unset(self):
        # 미설정 = 제한 없음. 그럴듯한 기본 영업시간을 넣으면 아직 설정하지 않은 매장의
        # 야간 시프트가 일일 리포트 인원 부족 검사에서 조용히 빠진다.
        async with async_session() as db:
            row = await db.scalar(
                select(SettingsRegistry).where(SettingsRegistry.key == STORE_OPERATING_HOURS_KEY)
            )
        assert row.default_value["all"] == {}
        assert row.default_value["closed"] == []
        assert resolve_day_range(row.default_value, 0) is None

    async def test_no_24_plus_notation_in_defaults(self):
        async with async_session() as db:
            rows = (
                await db.execute(
                    select(SettingsRegistry).where(
                        SettingsRegistry.key.in_([STORE_OPERATING_HOURS_KEY, SCHEDULE_RANGE_KEY])
                    )
                )
            ).scalars().all()
        for row in rows:
            for entry in [row.default_value.get("all"), *row.default_value.get("per_day", {}).values()]:
                if not entry:
                    continue
                for field in ("start", "end"):
                    hour = int(str(entry[field]).split(":")[0])
                    assert hour <= 23, f"{row.key}.{field} uses 24+ notation"

    async def test_schedule_range_label_reflects_new_meaning(self):
        # 키 이름은 옛 것(표시 범위)이고 의미는 D2-4(직원 근무 가능 시간대)다.
        # 사용자는 label 만 보므로 label 이 의미를 담아야 한다.
        async with async_session() as db:
            row = await db.scalar(
                select(SettingsRegistry).where(SettingsRegistry.key == SCHEDULE_RANGE_KEY)
            )
        assert "Working Hours" in row.label


class TestOperatingHoursColumnRemoved:
    async def test_stores_table_has_no_operating_hours_column(self):
        # 출처가 둘이면 다시 갈라진다. 컬럼이 되살아나면 여기서 막는다.
        assert not hasattr(Store, "operating_hours")


class TestDayStartSnapshotOnCreate:
    async def _create_store(self, organization_id: UUID, name: str) -> UUID:
        async with async_session() as db:
            created = await StoreService().create_store(
                db, organization_id, StoreCreate(name=name)
            )
        return UUID(created.id)

    async def test_copies_org_default(self, seed_organization: dict):
        org_id = seed_organization["id"]
        async with async_session() as db:
            original = await db.scalar(
                select(Organization.day_start_time).where(Organization.id == org_id)
            )
            await db.execute(
                Organization.__table__.update()
                .where(Organization.id == org_id)
                .values(day_start_time=time(17, 0))
            )
            await db.commit()

        store_id = None
        try:
            store_id = await self._create_store(org_id, "TZ Snapshot Store")
            async with async_session() as db:
                snapshot = await db.scalar(
                    select(Store.day_start_time).where(Store.id == store_id)
                )
            assert snapshot == {"all": "17:00"}

            # 스냅샷이지 cascade 가 아니다 — 조직 기본값을 바꿔도 기존 매장은 그대로.
            async with async_session() as db:
                await db.execute(
                    Organization.__table__.update()
                    .where(Organization.id == org_id)
                    .values(day_start_time=time(3, 0))
                )
                await db.commit()
            async with async_session() as db:
                unchanged = await db.scalar(
                    select(Store.day_start_time).where(Store.id == store_id)
                )
            assert unchanged == {"all": "17:00"}
        finally:
            # 원복 — 경계가 남으면 뒤따르는 테스트의 work_date/late 판정이 전부 달라진다.
            async with async_session() as db:
                if store_id is not None:
                    await db.execute(delete(Store).where(Store.id == store_id))
                await db.execute(
                    Organization.__table__.update()
                    .where(Organization.id == org_id)
                    .values(day_start_time=original)
                )
                await db.commit()

    async def test_org_unset_leaves_store_unset(self, seed_organization: dict):
        org_id = seed_organization["id"]
        async with async_session() as db:
            original = await db.scalar(
                select(Organization.day_start_time).where(Organization.id == org_id)
            )
            await db.execute(
                Organization.__table__.update()
                .where(Organization.id == org_id)
                .values(day_start_time=None)
            )
            await db.commit()

        store_id = None
        try:
            store_id = await self._create_store(org_id, "TZ Snapshot Store 2")
            async with async_session() as db:
                snapshot = await db.scalar(
                    select(Store.day_start_time).where(Store.id == store_id)
                )
            assert snapshot is None  # 런타임 기본값(06:00)에 맡긴다
        finally:
            async with async_session() as db:
                if store_id is not None:
                    await db.execute(delete(Store).where(Store.id == store_id))
                await db.execute(
                    Organization.__table__.update()
                    .where(Organization.id == org_id)
                    .values(day_start_time=original)
                )
                await db.commit()
