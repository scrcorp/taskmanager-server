"""푸시 딥링크 경로 — staff 앱 라우터와의 정합성.

왜 이 테스트가 필요한가:
    `_REFERENCE_ROUTES` 는 서버가 만들지만 해석은 staff 앱이 한다. 앱에 없는 경로를
    보내도 서버는 아무 이상이 없고, 앱은 errorBuilder 가 없어 기본 에러 화면을 띄운다.
    즉 **알림을 눌러야만 드러나는 고장**이라 사람이 알아채기 어렵다.
    실제로 '/mytask'(체크리스트·업무)와 '/my/attendance' 가 존재하지 않아 깨져 있었다.

    그래서 앱 라우터 파일을 직접 읽어 대조한다. 앱에서 경로를 바꾸거나 지우면
    이 테스트가 깨져서 서버 쪽 매핑도 같이 고치게 된다.
"""

import re
from pathlib import Path

import pytest

from app.services.push_dispatch import _ALERTS_ROUTE, _REFERENCE_ROUTES, _route_for


class _FakeAlert:
    """_route_for 는 reference_type 만 본다 — DB 를 태울 이유가 없다."""

    def __init__(self, reference_type):
        self.reference_type = reference_type


_ROUTER_REL = Path("apps") / "staff" / "lib" / "config" / "router.dart"


def _find_router() -> Path | None:
    """staff 앱 라우터를 찾는다 — 메인 체크아웃과 워크트리 레이아웃 둘 다 지원.

        메인:     <ws>/server            → <ws>/app/apps/staff/...
        워크트리: <ws>/server/.claude/worktrees/<n> → <ws>/app/.claude/worktrees/<n>/apps/staff/...

    같은 워크트리의 app 을 먼저 본다. 메인 app 을 보면 브랜치가 달라 엉뚱한
    라우터와 대조하게 된다.
    """
    server_root = Path(__file__).resolve().parents[1]
    candidates = []

    # 워크트리: 같은 이름의 app 워크트리를 우선
    if server_root.parent.name == "worktrees":
        name = server_root.name
        # <ws>/server/.claude/worktrees/<n> 기준: parents[3] 이 <ws>
        workspace = server_root.parents[3]
        candidates.append(workspace / "app" / ".claude" / "worktrees" / name / _ROUTER_REL)
        candidates.append(workspace / "app" / _ROUTER_REL)
    else:
        candidates.append(server_root.parent / "app" / _ROUTER_REL)

    for c in candidates:
        if c.exists():
            return c
    return None


_ROUTER = _find_router()


def _app_routes() -> set[str]:
    """staff 앱 라우터에 선언된 경로 집합."""
    text = _ROUTER.read_text(encoding="utf-8")
    return set(re.findall(r"path:\s*'([^']+)'", text))


requires_app = pytest.mark.skipif(
    _ROUTER is None,
    reason="staff 앱 repo 가 형제 경로에 없다 (CI 에서 server 만 체크아웃한 경우)",
)


@requires_app
def test_app_router_was_found() -> None:
    """전제 — 라우터를 못 읽으면 아래 대조가 조용히 무의미해진다."""
    routes = _app_routes()
    assert len(routes) > 20, f"라우터 파싱이 이상하다: {routes}"
    assert "/home" in routes


@requires_app
@pytest.mark.parametrize("reference_type,route", sorted(_REFERENCE_ROUTES.items()))
def test_every_mapped_route_exists_in_app(reference_type: str, route: str) -> None:
    """매핑된 경로는 전부 앱 라우터에 실재해야 한다."""
    assert route in _app_routes(), (
        f"reference_type={reference_type!r} 이 없는 경로 {route!r} 로 보낸다. "
        "알림을 누르면 에러 화면이 뜬다 — router.dart 와 맞출 것."
    )


@requires_app
def test_fallback_route_exists_in_app() -> None:
    assert _ALERTS_ROUTE in _app_routes()


def test_unknown_reference_type_falls_back_to_alerts() -> None:
    """모르는 종류는 홈이 아니라 알림함으로 — 방금 받은 알림을 거기서 볼 수 있다."""
    assert _route_for(_FakeAlert("something_new")) == _ALERTS_ROUTE


def test_missing_reference_type_falls_back_to_alerts() -> None:
    assert _route_for(_FakeAlert(None)) == _ALERTS_ROUTE


def test_checklist_and_task_go_to_different_screens() -> None:
    """둘 다 '/mytask' 로 뭉쳐 있던 회귀를 막는다 — 앱에서는 별개 화면이다."""
    checklist = _route_for(_FakeAlert("checklist_instance"))
    task = _route_for(_FakeAlert("task"))
    assert checklist != task, "체크리스트와 업무는 서로 다른 화면이다"
    assert checklist == "/work"
    assert task == "/tasks"


def test_all_routes_are_absolute_paths() -> None:
    """상대 경로나 외부 URL 이 섞이면 클라이언트 가드에 막혀 조용히 무시된다."""
    for reference_type, route in _REFERENCE_ROUTES.items():
        assert route.startswith("/"), f"{reference_type}: {route}"
        assert not route.startswith("//"), f"{reference_type}: {route} (외부로 나간다)"
