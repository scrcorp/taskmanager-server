"""초기 설정 HTML 페이지 — 최초 관리자 계정 생성 폼.

Setup HTML page — Serves a simple form for initial admin account creation.
The form POSTs to /api/v1/admin/auth/setup (admin auth router).
"""

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.models.organization import Organization

router: APIRouter = APIRouter()

SETUP_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TaskManager Setup</title>
<style>
body{font-family:system-ui,sans-serif;background:#111;color:#eee;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}
.card{background:#1a1a2e;border:1px solid #333;border-radius:12px;padding:40px;width:360px}
h2{text-align:center;margin:0 0 24px}
label{display:block;font-size:13px;color:#aaa;margin-bottom:4px}
input{width:100%;padding:10px;border:1px solid #333;border-radius:6px;background:#111;color:#eee;font-size:14px;box-sizing:border-box;margin-bottom:16px}
input:focus{outline:none;border-color:#6c5ce7}
button{width:100%;padding:12px;border:none;border-radius:6px;background:#6c5ce7;color:#fff;font-size:14px;font-weight:bold;cursor:pointer}
button:hover{background:#7c6df0}
.msg{padding:10px;border-radius:6px;font-size:13px;margin-bottom:16px;text-align:center}
.err{background:#ff6b6b22;color:#ff6b6b}
.ok{background:#00b89422;color:#00b894}
</style>
</head>
<body>
<div class="card">
<h2>TaskManager Setup</h2>
{{MESSAGE}}
<form method="post" action="/api/v1/admin/auth/setup">
<label>Organization Name</label>
<input name="organization_name" required placeholder="My Company">
<label>Admin Username</label>
<input name="username" required placeholder="admin">
<label>Admin Password</label>
<input name="password" type="password" required placeholder="password">
<button type="submit">Create</button>
</form>
</div>
</body>
</html>"""


def _render(message: str = "") -> HTMLResponse:
    return HTMLResponse(SETUP_HTML.replace("{{MESSAGE}}", message))


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    """초기 설정 페이지를 반환합니다."""
    count = (await db.execute(select(func.count()).select_from(Organization))).scalar() or 0
    if count > 0:
        return _render('<div class="msg ok">Setup already completed. Use /docs or admin app to log in.</div>')
    return _render()
