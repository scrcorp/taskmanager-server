"""알림 이메일 CTA 링크 빌더.

수신자에 따라 목적지가 다르다 — 콘솔은 SV 미만의 로그인을 막으므로, staff 수신자에게
콘솔 링크를 보내면 로그인 화면에서 막힌다. priority 를 넘겨 자동으로 갈라준다.

경로는 각 프론트의 라우트 정의와 짝을 이룬다. 라우트를 바꾸면 여기도 같이 고칠 것:
  console  : console/src/app/(dashboard)/...
  staff app: app/apps/staff/lib/config/router.dart
"""

from uuid import UUID

from app.config import settings
from app.core.permissions import SV_PRIORITY

# reference kind → (console 경로, staff app 경로). {id} 자리표시자.
_ROUTES: dict[str, tuple[str, str]] = {
    "issue_report": ("/reports/issues/{id}", "/issue-reports/{id}"),
    "daily_report": ("/daily-reports/{id}", "/daily-reports/{id}"),
    "checklist_instance": ("/checklists/instances/{id}", "/work/{id}"),
}


def _prefers_staff_app(recipient_priority: int | None) -> bool:
    """콘솔에 못 들어가는 수신자인가. priority 를 모르면 콘솔로 보낸다(기존 동작)."""
    return recipient_priority is not None and recipient_priority > SV_PRIORITY


def build_cta_url(
    kind: str,
    target_id: UUID | str,
    recipient_priority: int | None = None,
) -> str | None:
    """알림 대상으로 가는 절대 URL. 알 수 없는 kind 면 None (버튼 미표시)."""
    route = _ROUTES.get(kind)
    if not route:
        return None
    console_path, app_path = route

    if _prefers_staff_app(recipient_priority):
        base = (settings.STAFF_APP_BASE_URL or "").rstrip("/")
        if base:
            return f"{base}{app_path.format(id=target_id)}"

    base = (settings.ADMIN_BASE_URL or "").rstrip("/")
    if not base:
        return None
    return f"{base}{console_path.format(id=target_id)}"
