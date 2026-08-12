"""설정 upsert 경로 통합 테스트.

**이 파일이 생긴 이유** — org/store 설정 upsert 엔드포인트에 테스트가 **하나도 없었다**.
그래서 값 형태 검증(F3)을 붙이면서 헬퍼 함수가 파일에 들어가지 않은 채 커밋됐는데,
전체 스위트 1581건이 전부 통과했다. 브라우저에서 저장을 눌러보고서야 500 이 드러났다.

계약:
  - 정상 값 → 200 저장
  - 시간대 키의 형태 위반(24+ 표기 등) → 400, 저장 안 됨
  - 미등록 키 → 400
  - 그 외 키는 형태 검증 대상이 아니다(기존 키들의 자유 형태를 깨지 않기 위함)
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import delete

from app.database import async_session
from app.models.settings import OrgSetting

pytestmark = pytest.mark.asyncio

ORG_URL = "/api/v1/console/settings/org"
OPERATING_HOURS = "store.operating_hours"
RANGE_KEY = "schedule.range"


def _valid_hours(**over):
    base = {
        "mode": "all",
        "all": {"start": "11:00", "end": "02:00", "end_offset_days": 1},
        "per_day": {},
        "closed": ["mon"],
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
async def _cleanup():
    """이 파일이 만든 org 설정 행을 원복 — 남기면 다른 테스트의 resolve 결과가 바뀐다."""
    yield
    async with async_session() as db:
        await db.execute(delete(OrgSetting).where(
            OrgSetting.key.in_([OPERATING_HOURS, RANGE_KEY, "schedule.max_shift_hours"]),
        ))
        await db.commit()


class TestUpsertWorks:
    async def test_scalar_value_saves(self, async_client: AsyncClient, admin_headers):
        """가장 단순한 경로 — 이게 깨지면 설정 화면 전체가 죽는다."""
        resp = await async_client.put(ORG_URL, headers=admin_headers, json={
            "key": "schedule.max_shift_hours", "value": 9, "force_locked": False,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["value"] == 9

    async def test_operating_hours_saves(self, async_client: AsyncClient, admin_headers):
        resp = await async_client.put(ORG_URL, headers=admin_headers, json={
            "key": OPERATING_HOURS, "value": _valid_hours(), "force_locked": False,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["value"]["closed"] == ["mon"]

    async def test_unset_all_is_accepted(self, async_client: AsyncClient, admin_headers):
        """미설정(빈 객체)은 '영업시간 제한 없음'의 정상 표현이다 — 거절하면 안 된다."""
        resp = await async_client.put(ORG_URL, headers=admin_headers, json={
            "key": OPERATING_HOURS,
            "value": {"mode": "all", "all": {}, "per_day": {}, "closed": ["mon"]},
            "force_locked": False,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["value"]["all"] == {}


class TestValueShapeIsEnforced:
    """F3 — 형태가 깨진 값은 파서에서 조용히 '미설정'이 된다. 입구에서 막아야 한다."""

    async def test_24_plus_notation_is_rejected(self, async_client: AsyncClient, admin_headers):
        resp = await async_client.put(ORG_URL, headers=admin_headers, json={
            "key": OPERATING_HOURS,
            "value": _valid_hours(all={"start": "03:00", "end": "26:00"}),
            "force_locked": False,
        })
        assert resp.status_code == 400, resp.text
        assert "24" in resp.text

    async def test_bad_offset_is_rejected(self, async_client: AsyncClient, admin_headers):
        resp = await async_client.put(ORG_URL, headers=admin_headers, json={
            "key": RANGE_KEY,
            "value": _valid_hours(all={"start": "09:00", "end": "17:00", "end_offset_days": 2}),
            "force_locked": False,
        })
        assert resp.status_code == 400, resp.text

    async def test_rejected_value_is_not_persisted(self, async_client: AsyncClient, admin_headers):
        await async_client.put(ORG_URL, headers=admin_headers, json={
            "key": OPERATING_HOURS,
            "value": _valid_hours(all={"start": "03:00", "end": "26:00"}),
            "force_locked": False,
        })
        async with async_session() as db:
            row = await db.scalar(
                delete(OrgSetting).where(OrgSetting.key == OPERATING_HOURS).returning(OrgSetting.id)
            )
        assert row is None, "거절된 값이 저장돼 있다"

    async def test_unregistered_key_is_rejected(self, async_client: AsyncClient, admin_headers):
        resp = await async_client.put(ORG_URL, headers=admin_headers, json={
            "key": "not.a.real.key", "value": 1, "force_locked": False,
        })
        assert resp.status_code == 400, resp.text
