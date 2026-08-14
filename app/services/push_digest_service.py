"""미확인 알림 다이제스트 푸시 — 하루 한 번 "안 본 알림 N건" 을 모아 보낸다.

건별 즉시 푸시와 성격이 다르다:
    즉시 푸시는 "방금 이 일이 생겼다" 를 알린다. 앱을 안 봐도 알게 하는 게 목적.
    다이제스트는 "쌓인 걸 아직 안 봤다" 를 알린다. 즉시 푸시를 놓쳤거나
    (폰 꺼짐, 알림 스와이프) 무시한 경우를 잡는 안전망이다.

타임존:
    직원 생활 시간대에 맞춰야 하므로 **조직 타임존 기준 로컬 시각**으로 판단한다.
    스케줄러는 매시 정각에 깨어나, 각 조직의 로컬 시각이 설정된 시(hour)와
    같을 때만 그 조직 사용자에게 보낸다. UTC 고정 시각으로 쏘면 조직마다
    한밤중에 울린다.

중복 방지:
    같은 날 두 번 보내지 않는다. 별도 상태 테이블을 두지 않고 push_deliveries
    에 남은 그날의 digest 기록을 근거로 판단한다 — 발송 사실이 곧 상태다.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.alert_categories import is_push_enabled_for_type
from app.database import async_session
from app.models.alert import Alert
from app.models.organization import Organization
from app.models.push_delivery import PushDelivery
from app.models.push_subscription import PushSubscription
from app.models.user import User
from app.services.push_service import push_service

logger = logging.getLogger(__name__)

# 다이제스트로 세는 미확인 알림의 기간. 너무 길면 오래된 것까지 계속 알려 피로해진다.
DIGEST_LOOKBACK_DAYS = 7

# 이 발송의 alert_type 스냅샷 값. 중복 판정과 조회에 쓴다.
DIGEST_TYPE = "digest"


def _local_hour(tz_name: str) -> int:
    """해당 타임존의 현재 시(hour). 알 수 없는 tz 는 UTC 로 본다."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - 잘못된 tz 문자열이 잡을 막으면 안 된다
        tz = ZoneInfo("UTC")
    return datetime.now(tz).hour


def _local_day_start_utc(tz_name: str) -> datetime:
    """해당 타임존의 '오늘 0시' 를 UTC 절대시각으로. 중복 판정 경계로 쓴다."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    local_now = datetime.now(tz)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)


async def _already_sent_today(
    db: AsyncSession, user_id: UUID, day_start_utc: datetime
) -> bool:
    """오늘 이미 다이제스트를 보냈는지 — push_deliveries 기록이 곧 상태다."""
    result = await db.execute(
        select(func.count())
        .select_from(PushDelivery)
        .where(PushDelivery.user_id == user_id)
        .where(PushDelivery.alert_type == DIGEST_TYPE)
        .where(PushDelivery.created_at >= day_start_utc)
    )
    return int(result.scalar_one()) > 0


async def _unread_count(db: AsyncSession, user_id: UUID, since: datetime) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(Alert)
        .where(Alert.user_id == user_id)
        .where(Alert.is_read.is_(False))
        .where(Alert.created_at >= since)
    )
    return int(result.scalar_one())


def _digest_body(count: int) -> str:
    """UI 문구는 영어. 단수/복수만 구분한다."""
    if count == 1:
        return "You have 1 unread notification."
    return f"You have {count} unread notifications."


async def send_digests_for_organization(
    db: AsyncSession, org: Organization
) -> int:
    """조직 1곳의 대상자에게 다이제스트를 보낸다. 보낸 사람 수를 반환."""
    day_start = _local_day_start_utc(org.timezone)
    since = datetime.now(timezone.utc) - timedelta(days=DIGEST_LOOKBACK_DAYS)

    # 구독이 있는 사용자만 후보다 — 구독이 없으면 애초에 보낼 곳이 없다.
    candidates = (
        await db.execute(
            select(User)
            .where(User.organization_id == org.id)
            .where(User.is_active.is_(True))
            .where(
                User.id.in_(
                    select(PushSubscription.user_id).where(
                        PushSubscription.organization_id == org.id
                    )
                )
            )
        )
    ).scalars().all()

    sent = 0
    for user in candidates:
        # 다이제스트도 사용자가 끌 수 있어야 한다. 카테고리 매핑이 없는 type 이라
        # 기본은 켬이지만, 훅을 여기 두면 나중에 전용 토글을 붙이기 쉽다.
        if not is_push_enabled_for_type(user.alert_preferences, DIGEST_TYPE):
            continue
        if await _already_sent_today(db, user.id, day_start):
            continue

        count = await _unread_count(db, user.id, since)
        if count == 0:
            continue

        await push_service.send_to_user(
            db,
            user.id,
            title="HTM",
            body=_digest_body(count),
            url="/",
            tag=DIGEST_TYPE,
            organization_id=org.id,
            alert_type=DIGEST_TYPE,
        )
        sent += 1

    return sent


async def run_digest_tick(now_hour: Optional[int] = None) -> None:
    """스케줄러가 매시 정각에 호출. 로컬 시각이 설정 시와 같은 조직만 발송.

    스케줄러 잡은 예외가 새어 나가면 다음 실행까지 죽을 수 있으므로 삼킨다.
    """
    if not settings.push_enabled or not settings.PUSH_DIGEST_ENABLED:
        return

    target_hour = settings.PUSH_DIGEST_HOUR
    try:
        async with async_session() as db:
            orgs = (
                await db.execute(
                    select(Organization).where(Organization.deleted_at.is_(None))
                )
            ).scalars().all()

            total = 0
            for org in orgs:
                hour = now_hour if now_hour is not None else _local_hour(org.timezone)
                if hour != target_hour:
                    continue
                total += await send_digests_for_organization(db, org)

            if total:
                await db.commit()
                logger.info("[push_digest] sent to %d users", total)
            else:
                await db.rollback()
    except Exception as exc:  # noqa: BLE001 - 잡이 죽으면 이후 tick 이 안 돈다
        logger.exception("[push_digest] tick failed: %s", exc)
