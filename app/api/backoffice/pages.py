"""Backoffice 서버렌더 HTML — 인라인 문자열 (setup.py 패턴 따름, Jinja 미사용).

Server-rendered HTML for the backoffice. Dark theme matching the existing
setup page. All responses carry X-Robots-Tag: noindex so the secret surface is
never indexed even if a URL leaks.
"""

import html as _html

from fastapi.responses import HTMLResponse

# 공통 스타일 — Notion 라이트 테마 (warm paper 캔버스 + 흰 카드 + 단일 블루 + hairline)
_STYLE = """
:root{
  --canvas:#f6f5f4;--surface:#fff;--ink:#1a1a1a;--ink2:#31302e;--muted:#615d59;--faint:#a39e98;
  --hairline:#e6e6e6;--primary:#0075de;--primary-active:#005bab;
  --shadow1:0 .175px 1.04px rgba(0,0,0,.01),0 .8px 2.9px rgba(0,0,0,.02),0 2px 7.8px rgba(0,0,0,.027),0 4px 18px rgba(0,0,0,.04);
}
*{box-sizing:border-box}
body{font-family:Inter,-apple-system,system-ui,"Segoe UI",Helvetica,Arial,sans-serif;background:var(--canvas);color:var(--ink);margin:0;-webkit-font-smoothing:antialiased}
a{color:var(--primary);text-decoration:none}
a:hover{text-decoration:underline}
.center{display:flex;justify-content:center;align-items:center;min-height:100vh}
.card{background:var(--surface);border:1px solid var(--hairline);border-radius:12px;padding:36px;width:360px;box-shadow:var(--shadow1)}
h1{font-size:40px;font-weight:700;letter-spacing:-1px;margin:0 0 20px}
h2{font-size:26px;font-weight:700;letter-spacing:-.625px;margin:0 0 20px;text-align:center}
h3{font-size:22px;font-weight:700;letter-spacing:-.25px}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:6px;font-weight:500}
input{width:100%;padding:10px;border:1px solid #ddd;border-radius:4px;background:var(--surface);color:var(--ink);font-size:15px;margin-bottom:16px;font-family:inherit}
input:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px rgba(0,117,222,.12)}
button{font-family:inherit;border:none;border-radius:9999px;background:var(--primary);color:#fff;font-size:16px;font-weight:500;cursor:pointer;padding:11px 22px;transition:transform .08s ease,background .15s ease}
button:hover{background:var(--primary-active)}
button:active{transform:scale(.97)}
.card button{width:100%}
.msg{padding:10px;border-radius:8px;font-size:13px;margin-bottom:16px;text-align:center}
.err{background:#fdeaea;color:#c0392b}
.tag{font-size:12px;color:var(--faint);letter-spacing:.06em;text-transform:uppercase;text-align:center;margin-bottom:8px;font-weight:600}
/* shell */
.shell{display:flex;min-height:100vh}
.nav{width:240px;background:var(--surface);border-right:1px solid var(--hairline);padding:24px 16px;flex-shrink:0}
.nav .brand{font-weight:700;font-size:15px;margin-bottom:24px;letter-spacing:-.2px}
.nav a{display:block;padding:9px 12px;border-radius:8px;color:var(--ink2);font-size:15px;margin-bottom:4px}
.nav a:hover{background:var(--canvas);text-decoration:none}
.nav a.muted{color:var(--faint);cursor:default}
.nav a.muted:hover{background:none}
.main{flex:1;padding:32px 40px;max-width:1320px}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px}
.topbar .who{font-size:13px;color:var(--muted)}
.topbar form{display:inline}
.topbar button{background:var(--surface);color:var(--ink);border:1px solid var(--hairline);border-radius:8px;padding:7px 14px;font-size:15px;box-shadow:var(--shadow1)}
.topbar button:hover{background:var(--canvas)}
.muted-box{background:var(--surface);border:1px solid var(--hairline);border-radius:12px;padding:24px;color:var(--muted);box-shadow:var(--shadow1)}
.muted-box b,.muted-box code{color:var(--ink2)}
.section{margin-bottom:24px}
table{font-size:14px}
th{color:var(--muted);font-weight:600}
td{color:var(--ink2)}
/* ---- EMPID review (bucket cards + data tables) ---- */
.stats{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:20px}
.stat{background:var(--surface);border:1px solid var(--hairline);border-radius:10px;padding:12px 16px;box-shadow:var(--shadow1);min-width:88px}
.stat .n{font-size:24px;font-weight:700;letter-spacing:-.5px;line-height:1}
.stat .l{font-size:11.5px;color:var(--muted);margin-top:5px;font-weight:500}
.toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:22px}
.btn-dl{display:inline-flex;align-items:center;gap:7px;padding:10px 18px;background:#107c41;color:#fff;border-radius:9999px;font-size:14px;font-weight:600}
.btn-dl:hover{background:#0c6133;text-decoration:none}
.jump{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.jump a{font-size:12px;padding:5px 11px;border:1px solid var(--hairline);border-radius:9999px;color:var(--ink2);background:var(--surface)}
.jump a:hover{background:var(--canvas);text-decoration:none}
.bucket{background:var(--surface);border:1px solid var(--hairline);border-radius:12px;box-shadow:var(--shadow1);margin-bottom:18px;overflow:hidden}
.bhead{display:flex;align-items:center;gap:10px;padding:15px 20px;font-weight:700;font-size:16px;border-bottom:1px solid var(--hairline)}
.bucket>summary{display:flex;align-items:center;gap:10px;padding:15px 20px;font-weight:700;font-size:16px;cursor:pointer;list-style:none}
.bucket>summary::-webkit-details-marker{display:none}
.bucket[open]>summary{border-bottom:1px solid var(--hairline)}
.badge{font-size:12px;font-weight:700;min-width:22px;text-align:center;padding:2px 8px;border-radius:9999px;color:#fff}
.bsub{font-weight:400;font-size:12.5px;color:var(--muted)}
.bbody{padding:6px 20px 16px}
.dtable{width:100%;border-collapse:collapse;font-size:13.5px}
.dtable th{text-align:left;color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;padding:9px 12px;border-bottom:1px solid var(--hairline)}
.dtable td{padding:10px 12px;border-bottom:1px solid #f0efed;vertical-align:top;color:var(--ink2)}
.dtable tr:last-child td{border-bottom:none}
.dtable tbody tr:hover{background:var(--canvas)}
.dtable .num{font-variant-numeric:tabular-nums;font-weight:600}
.pill{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:6px;background:#eaf3fc;color:#0075de;font-variant-numeric:tabular-nums}
.pill-warn{background:#fdecea;color:#c0392b}
.hint{color:var(--muted);font-size:12px}
.empty{color:var(--faint);font-style:italic;padding:6px 0;font-size:13px}
select.sel{padding:6px 10px;background:#fff;color:var(--ink);border:1px solid #ddd;border-radius:8px;font-family:inherit;font-size:13px;max-width:280px}
.confirm-bar{display:flex;align-items:center;gap:14px;margin:6px 0 26px}
.confirm-bar .hint{margin:0}
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
    ("Organizations", "/tools/orgs"),
    ("Users", None),
    ("Tools · EMPID", "/tools/empid"),
    ("Changelog", "/tools/changelog"),
]


def _nav(base: str, active: str) -> str:
    items = []
    for label, path in _NAV:
        if path is None:
            items.append(f"<a class='muted'>{_html.escape(label)} <span style='font-size:10px'>soon</span></a>")
        else:
            cls = " style='background:#eaf3fc;color:#0075de;font-weight:600'" if path == active else ""
            items.append(f"<a href='{base}{path}'{cls}>{_html.escape(label)}</a>")
    return (
        "<div class='nav'><div class='brand'>⬡ Backoffice</div>"
        + "".join(items)
        + "</div>"
    )


def shell(base: str, admin: str, active: str, title: str, content: str,
          page_title: str = "HTM Backoffice") -> HTMLResponse:
    """공통 셸 — 좌측 nav + topbar(로그아웃) + 본문. 도구 페이지가 재사용."""
    body = (
        "<div class='shell'>"
        f"{_nav(base, active)}"
        "<div class='main'>"
        "<div class='topbar'>"
        f"<h1>{_html.escape(title)}</h1>"
        f"<div><span class='who'>signed in as <b>{_html.escape(admin)}</b></span> "
        f"<form method='post' action='{base}/logout'><button type='submit'>Sign out</button></form></div>"
        "</div>"
        f"{content}"
        "</div></div>"
    )
    return render(page_title, body)


def dashboard_html(base: str, admin: str) -> HTMLResponse:
    """대시보드 — 셸 + 도구 안내."""
    content = (
        "<div class='section'><div class='muted-box'>"
        "Operator console shell is live. Tools:<br><br>"
        f"• <a href='{base}/tools/orgs'><b>Organizations</b></a> — list every org + create new (org bootstrap)<br>"
        "• <b>Users</b> — cross-org lookup (soon)<br>"
        f"• <a href='{base}/tools/empid'><b>Tools · EMPID Reconciliation</b></a> — legacy employee-number import"
        "</div></div>"
    )
    return shell(base, admin, "/dashboard", "Dashboard", content, "HTM Backoffice — Dashboard")
