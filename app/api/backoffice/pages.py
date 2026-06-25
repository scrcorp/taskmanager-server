"""Backoffice 서버렌더 HTML — 인라인 문자열 (setup.py 패턴 따름, Jinja 미사용).

Server-rendered HTML for the backoffice. Dark theme matching the existing
setup page. All responses carry X-Robots-Tag: noindex so the secret surface is
never indexed even if a URL leaks.
"""

import html as _html

from fastapi.responses import HTMLResponse

# 공통 스타일 — setup.py 다크테마 재사용 + 셸 레이아웃 약간 확장
_STYLE = """
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#0f0f17;color:#e8e8ec;margin:0}
a{color:#8b7df0;text-decoration:none}
.center{display:flex;justify-content:center;align-items:center;min-height:100vh}
.card{background:#1a1a2e;border:1px solid #2a2a40;border-radius:12px;padding:36px;width:360px}
h1,h2{margin:0 0 20px}
h2{text-align:center}
label{display:block;font-size:13px;color:#9a9ab0;margin-bottom:4px}
input{width:100%;padding:10px;border:1px solid #2a2a40;border-radius:6px;background:#0f0f17;color:#e8e8ec;font-size:14px;margin-bottom:16px}
input:focus{outline:none;border-color:#6c5ce7}
button{width:100%;padding:12px;border:none;border-radius:6px;background:#6c5ce7;color:#fff;font-size:14px;font-weight:600;cursor:pointer}
button:hover{background:#7c6df0}
.msg{padding:10px;border-radius:6px;font-size:13px;margin-bottom:16px;text-align:center}
.err{background:#ff6b6b22;color:#ff8787}
.tag{font-size:11px;color:#6a6a80;letter-spacing:.08em;text-transform:uppercase;text-align:center;margin-bottom:8px}
/* shell */
.shell{display:flex;min-height:100vh}
.nav{width:220px;background:#15151f;border-right:1px solid #2a2a40;padding:24px 16px;flex-shrink:0}
.nav .brand{font-weight:700;font-size:15px;margin-bottom:24px}
.nav a{display:block;padding:9px 12px;border-radius:6px;color:#c8c8d4;font-size:14px;margin-bottom:4px}
.nav a:hover{background:#22223a}
.nav a.muted{color:#5a5a70;cursor:default}
.nav a.muted:hover{background:none}
.main{flex:1;padding:32px 40px}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px}
.topbar .who{font-size:13px;color:#9a9ab0}
.topbar form{display:inline}
.topbar button{width:auto;padding:7px 14px;background:#2a2a40}
.topbar button:hover{background:#3a3a55}
.muted-box{background:#15151f;border:1px dashed #2a2a40;border-radius:10px;padding:24px;color:#7a7a90}
.section{margin-bottom:24px}
"""


def _doc(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='robots' content='noindex, nofollow'>"
        f"<title>{_html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def render(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    """noindex 헤더를 단 HTMLResponse 생성."""
    return HTMLResponse(
        _doc(title, body),
        status_code=status_code,
        headers={"X-Robots-Tag": "noindex, nofollow"},
    )


def login_html(base: str, message: str = "", status_code: int = 200) -> HTMLResponse:
    """운영자 로그인 페이지."""
    msg = f"<div class='msg err'>{_html.escape(message)}</div>" if message else ""
    body = (
        "<div class='center'><div class='card'>"
        "<div class='tag'>HTM Backoffice</div>"
        "<h2>Operator Login</h2>"
        f"{msg}"
        f"<form method='post' action='{base}/login'>"
        "<label>Username</label><input name='username' required autofocus autocomplete='off'>"
        "<label>Password</label><input name='password' type='password' required autocomplete='off'>"
        "<button type='submit'>Sign in</button>"
        "</form></div></div>"
    )
    return render("HTM Backoffice", body, status_code)


# 좌측 nav 메뉴 — (라벨, 경로 또는 None=비활성/곧추가)
_NAV = [
    ("Dashboard", "/dashboard"),
    ("Organizations", None),
    ("Users", None),
    ("Tools · EMPID", None),
]


def _nav(base: str, active: str) -> str:
    items = []
    for label, path in _NAV:
        if path is None:
            items.append(f"<a class='muted'>{_html.escape(label)} <span style='font-size:10px'>soon</span></a>")
        else:
            items.append(f"<a href='{base}{path}'>{_html.escape(label)}</a>")
    return (
        "<div class='nav'><div class='brand'>⬡ Backoffice</div>"
        + "".join(items)
        + "</div>"
    )


def dashboard_html(base: str, admin: str) -> HTMLResponse:
    """대시보드 셸 — 좌측 nav + 콘텐츠. P1은 빈 셸(앞으로 도구 입주)."""
    body = (
        "<div class='shell'>"
        f"{_nav(base, 'dashboard')}"
        "<div class='main'>"
        "<div class='topbar'>"
        "<h1>Dashboard</h1>"
        f"<div><span class='who'>signed in as <b>{_html.escape(admin)}</b></span> "
        f"<form method='post' action='{base}/logout'><button type='submit'>Sign out</button></form></div>"
        "</div>"
        "<div class='section'><div class='muted-box'>"
        "Operator console shell is live. Tools will mount here:<br><br>"
        "• <b>Organizations</b> — list/drill into every org<br>"
        "• <b>Users</b> — cross-org lookup<br>"
        "• <b>Tools · EMPID Reconciliation</b> — legacy employee-number import"
        "</div></div>"
        "</div></div>"
    )
    return render("HTM Backoffice — Dashboard", body)
