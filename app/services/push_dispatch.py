"""알림(alert) 생성 → 웹 푸시 발송 연결.

한 사건이 두 채널로 나간다:

    스케줄 승인됨
      ├─ alerts 테이블 INSERT       → 앱 알림함 (항상)
      └─ 웹 푸시                    → 폰 배너 (푸시 켠 사람만)

여기서 풀어야 할 문제가 두 개다.

1. **커밋 전에 보내면 안 된다.**
   알림 행을 만든 트랜잭션이 롤백될 수 있다. 그 상태에서 푸시가 나가면
   "일어나지 않은 일" 을 알린 셈이 된다. 그래서 백그라운드 작업이 **자기 세션으로
   해당 alert 행을 다시 읽어** 실제로 커밋됐는지 확인한 뒤에만 보낸다.
   (아직 커밋 전이면 짧게 재시도하고, 끝내 없으면 롤백으로 보고 포기한다)

2. **응답을 막으면 안 된다.**
   푸시는 외부 HTTPS 왕복이라 느리고 실패한다. 매니저가 "승인" 을 눌렀는데
   화면이 멈추면 안 되므로 요청 처리와 분리된 태스크로 돌린다.
   실패는 로그만 남기고 삼킨다 — 푸시 실패가 본 작업을 되돌리면 안 된다.
"""

import asyncio
import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import select

from app.core.alert_categories import is_push_enabled_for_type, label_for_type
from app.database import async_session
from app.models.alert import Alert
from app.models.push_delivery import (
    DELIVERY_SKIPPED,
    SKIP_PREFERENCE_OFF,
    PushDelivery,
)
from app.models.user import User
from app.services.push_service import push_service

logger = logging.getLogger(__name__)

# 커밋을 기다리며 alert 행을 다시 읽어보는 간격(초). 합계 ~1.4초.
_COMMIT_POLL_DELAYS = (0.2, 0.4, 0.8)

# reference_type → 앱 내 이동 경로. 없으면 홈으로.
_REFERENCE_ROUTES: dict[str, str] = {
    "schedule": "/schedule",
    "checklist_instance": "/mytask",
    "daily_report": "/daily-reports",
    "notice": "/notices",
    "task": "/mytask",
    "warning": "/my/warnings",
    "attendance": "/my/attendance",
}


def _route_for(alert: Alert) -> str:
    if not alert.reference_type:
        return "/"
    return _REFERENCE_ROUTES.get(alert.reference_type, "/")


async def _send_for_alert(alert_id: UUID) -> None:
    """alert 이 실제로 커밋됐는지 확인한 뒤 푸시를 보낸다."""
    for delay in _COMMIT_POLL_DELAYS:
        await asyncio.sleep(delay)
        async with async_session() as db:
            alert: Optional[Alert] = await db.get(Alert, alert_id)
            if alert is None:
                # 아직 커밋 전일 수 있다 — 다음 간격에 다시 본다.
                continue

            # 수신자의 푸시 선호 확인. 카테고리별로 끌 수 있다(미설정 = 켬).
            user = (
                await db.execute(select(User).where(User.id == alert.user_id))
            ).scalar_one_or_none()
            if user is None:
                return
            if not is_push_enabled_for_type(user.alert_preferences, alert.type):
                # 보내지 않은 사실도 남긴다 — 나중에 "왜 안 왔냐" 에 답할 근거다.
                # (설정 이력과 짝을 이뤄 "본인이 꺼둔 상태였다" 를 보인다)
                db.add(
                    PushDelivery(
                        organization_id=alert.organization_id,
                        user_id=alert.user_id,
                        alert_id=alert.id,
                        alert_type=alert.type,
                        status=DELIVERY_SKIPPED,
                        skip_reason=SKIP_PREFERENCE_OFF,
                        title=label_for_type(alert.type)[:200],
                        body=alert.message[:500],
                    )
                )
                await db.commit()
                return

            result = await push_service.send_to_user(
                db,
                alert.user_id,
                title=label_for_type(alert.type),
                body=alert.message,
                url=_route_for(alert),
                # 같은 종류 알림이 배너로 쌓이지 않게 alert type 단위로 교체한다.
                # (모델 속성명은 alert_type 이 아니라 type — 리포지토리 인자명과 다르다)
                tag=alert.type,
                organization_id=alert.organization_id,
                alert_id=alert.id,
                alert_type=alert.type,
            )
            await db.commit()
            if result.attempted:
                logger.info(
                    "push for alert %s: sent=%d failed=%d removed=%d",
                    alert_id, result.sent, result.failed, result.removed,
                )
            return

    logger.debug("alert %s never committed — push skipped", alert_id)


def dispatch_alert_push(alert: Optional[Alert]) -> None:
    """알림 1건에 대한 푸시를 백그라운드로 예약한다.

    호출자는 결과를 기다리지 않는다. 예외도 밖으로 나가지 않는다.
    이벤트 루프가 없는 환경(동기 테스트 등)에서는 조용히 아무것도 하지 않는다.
    """
    if alert is None or alert.id is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_send_for_alert(alert.id))
    # 태스크 참조를 잃으면 GC 될 수 있다. 완료 시 예외를 삼키고 로그만 남긴다.
    task.add_done_callback(_log_task_result)


def _log_task_result(task: "asyncio.Task[None]") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("push dispatch task failed: %s", exc)
