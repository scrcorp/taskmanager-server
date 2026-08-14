"""웹 푸시 발송 서비스 — VAPID 로 서명해 브라우저 푸시 중계 서버에 직접 쏜다.

인앱 알림(`alerts` 행 INSERT)과 근본적으로 다른 일이다:
    - 인앱: DB 에 쌓아두고 앱이 열릴 때 가져간다. 실패할 일이 없다.
    - 푸시: 우리 서버가 외부(구글/애플/모질라)로 HTTPS 요청을 보낸다.
            구독 1건당 요청 1번. 느리고, 실패하고, 정리가 필요하다.

핵심 규칙 두 가지:

1. **푸시 실패가 본 작업을 망가뜨리면 안 된다.**
   스케줄 승인 중 푸시가 실패해도 승인은 성공해야 한다. 그래서 이 모듈의
   공개 함수는 예외를 밖으로 던지지 않고 결과를 집계해 돌려준다.

2. **404/410 은 확정 사망이므로 즉시 구독을 지운다.**
   사용자가 홈 화면 아이콘을 지우거나 권한을 차단해도 서버는 통보받지 못한다.
   발송 실패가 그 사실을 알 수 있는 유일한 사후 경로다.

pywebpush 는 동기 블로킹 HTTP 라이브러리라 이벤트 루프를 막는다.
반드시 스레드로 빼서 호출한다(`asyncio.to_thread`).
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from pywebpush import WebPushException, webpush
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.push_delivery import (
    DELIVERY_ACCEPTED,
    DELIVERY_FAILED,
    DELIVERY_GONE,
    DELIVERY_SKIPPED,
    SKIP_NO_SUBSCRIPTION,
    SKIP_PUSH_DISABLED,
    PushDelivery,
)
from app.models.push_subscription import PushSubscription

logger = logging.getLogger(__name__)

# 중계 서버가 알림을 보관해 줄 시간(초). 오프라인 기기가 이 안에 켜지면 받는다.
DEFAULT_TTL = 12 * 60 * 60

# 구독이 죽었다는 확정 신호. 그 외 4xx/5xx 는 일시적일 수 있어 행을 남긴다.
DEAD_SUBSCRIPTION_STATUSES = {404, 410}


@dataclass
class _DeliveryContext:
    """발송 기록에 함께 남길 맥락. 결과와 무관하게 모든 행에 붙는다."""

    organization_id: Optional[UUID]
    user_id: UUID
    alert_id: Optional[UUID]
    alert_type: Optional[str]
    title: str
    body: str


@dataclass
class PushResult:
    """발송 집계 결과 — 호출자가 로깅/판단에 쓴다."""

    sent: int = 0
    failed: int = 0
    removed: int = 0  # 죽어서 삭제한 구독 수
    errors: list[str] = field(default_factory=list)

    @property
    def attempted(self) -> int:
        return self.sent + self.failed


def _vapid_claims() -> dict[str, str]:
    """VAPID JWT 클레임 — sub 는 중계 서버가 문제 시 연락할 곳."""
    return {"sub": settings.VAPID_SUBJECT}


def _send_one_blocking(subscription_info: dict[str, Any], payload: str) -> None:
    """pywebpush 동기 호출 1건. 스레드에서 실행할 것.

    성공하면 조용히 반환, 실패하면 WebPushException 을 던진다.
    """
    webpush(
        subscription_info=subscription_info,
        data=payload,
        vapid_private_key=settings.VAPID_PRIVATE_KEY,
        vapid_claims=_vapid_claims(),
        ttl=DEFAULT_TTL,
    )


def _status_of(exc: WebPushException) -> Optional[int]:
    """예외에서 HTTP 상태코드를 뽑는다. 응답이 없으면 None(네트워크 오류 등)."""
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) if response is not None else None


class PushService:
    """구독 조회 → 발송 → 죽은 구독 정리까지 한 묶음."""

    async def send_to_user(
        self,
        db: AsyncSession,
        user_id: UUID,
        *,
        title: str,
        body: str,
        url: str = "/",
        tag: Optional[str] = None,
        organization_id: Optional[UUID] = None,
        alert_id: Optional[UUID] = None,
        alert_type: Optional[str] = None,
    ) -> PushResult:
        """한 사용자의 **모든 기기**에 푸시를 보낸다.

        Args:
            db: 비동기 DB 세션
            user_id: 수신자
            title: 알림 제목
            body: 알림 본문
            url: 알림을 탭했을 때 열 앱 내 경로
            tag: 같은 tag 의 알림은 기기에서 덮어쓰기된다(중복 쌓임 방지)
            organization_id: 기록용. 생략하면 구독 행에서 가져온다.
            alert_id/alert_type: 이 발송을 유발한 알림 (있으면 기록에 남는다)

        Returns:
            PushResult: 성공/실패/삭제 집계. **예외를 던지지 않는다.**

        시도는 모두 push_deliveries 에 남는다 — 보내지 않은 경우(skipped)도
        포함한다. "그날 알림이 갔었나" 에 답하려면 안 보낸 사실도 근거가 된다.
        """
        ctx = _DeliveryContext(
            organization_id=organization_id,
            user_id=user_id,
            alert_id=alert_id,
            alert_type=alert_type,
            title=title,
            body=body,
        )

        if not settings.push_enabled:
            # 키가 없으면 기능 자체가 꺼진 것 — 조용히 넘어간다(로컬 개발 등).
            self._record_skip(db, ctx, SKIP_PUSH_DISABLED)
            return PushResult()

        result = await db.execute(
            select(PushSubscription).where(PushSubscription.user_id == user_id)
        )
        subscriptions = list(result.scalars().all())
        if not subscriptions:
            self._record_skip(db, ctx, SKIP_NO_SUBSCRIPTION)
            return PushResult()

        if ctx.organization_id is None:
            ctx.organization_id = subscriptions[0].organization_id

        payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
        return await self._fan_out(db, subscriptions, payload, ctx)

    def _record_skip(
        self, db: AsyncSession, ctx: "_DeliveryContext", reason: str
    ) -> None:
        """보내지 않은 사실도 남긴다 — 조용한 무발송이 가장 설명하기 어렵다."""
        if ctx.organization_id is None:
            # 조직을 모르면 기록할 수 없다(멀티테넌트 격리 컬럼이 NOT NULL).
            return
        db.add(
            PushDelivery(
                organization_id=ctx.organization_id,
                user_id=ctx.user_id,
                alert_id=ctx.alert_id,
                alert_type=ctx.alert_type,
                status=DELIVERY_SKIPPED,
                skip_reason=reason,
                title=ctx.title[:200],
                body=ctx.body[:500],
            )
        )

    def _record(
        self,
        db: AsyncSession,
        ctx: "_DeliveryContext",
        sub: PushSubscription,
        *,
        status: str,
        http_status: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        db.add(
            PushDelivery(
                organization_id=ctx.organization_id or sub.organization_id,
                user_id=ctx.user_id,
                alert_id=ctx.alert_id,
                alert_type=ctx.alert_type,
                subscription_endpoint=sub.endpoint,
                status=status,
                http_status=http_status,
                error=(error or None) and error[:500],
                title=ctx.title[:200],
                body=ctx.body[:500],
            )
        )

    async def _fan_out(
        self,
        db: AsyncSession,
        subscriptions: list[PushSubscription],
        payload: str,
        ctx: "_DeliveryContext",
    ) -> PushResult:
        """구독 목록에 병렬 발송하고 결과에 따라 DB 를 정리한다."""
        outcome = PushResult()
        dead_ids: list[UUID] = []

        async def attempt(sub: PushSubscription) -> None:
            info = {
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
            }
            try:
                await asyncio.to_thread(_send_one_blocking, info, payload)
            except WebPushException as exc:
                status = _status_of(exc)
                if status in DEAD_SUBSCRIPTION_STATUSES:
                    # 확정 사망 — 실패로 세지 않고 정리 대상에 넣는다.
                    dead_ids.append(sub.id)
                    outcome.removed += 1
                    self._record(db, ctx, sub, status=DELIVERY_GONE, http_status=status)
                    logger.info(
                        "push subscription gone (%s), removing: %s", status, sub.endpoint[:60]
                    )
                    return
                outcome.failed += 1
                outcome.errors.append(f"{status or 'network'}: {exc}")
                sub.failure_count += 1
                self._record(
                    db, ctx, sub, status=DELIVERY_FAILED,
                    http_status=status, error=str(exc),
                )
                logger.warning("push send failed (%s): %s", status, exc)
            except Exception as exc:  # noqa: BLE001 - 발송 실패가 본 작업을 막으면 안 된다
                outcome.failed += 1
                outcome.errors.append(str(exc))
                self._record(db, ctx, sub, status=DELIVERY_FAILED, error=str(exc))
                logger.exception("push send crashed: %s", exc)
            else:
                outcome.sent += 1
                sub.last_success_at = datetime.now(timezone.utc)
                sub.failure_count = 0
                # 'accepted' 이지 'delivered' 가 아니다 — 중계 서버가 받았을 뿐,
                # 사용자가 봤는지는 어떤 방법으로도 알 수 없다.
                self._record(db, ctx, sub, status=DELIVERY_ACCEPTED)

        await asyncio.gather(*(attempt(s) for s in subscriptions))

        if dead_ids:
            await db.execute(
                delete(PushSubscription).where(PushSubscription.id.in_(dead_ids))
            )
        await db.flush()
        return outcome


push_service = PushService()
