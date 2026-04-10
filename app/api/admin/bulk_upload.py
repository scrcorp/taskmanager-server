"""Bulk upload 라우터 — 직원/스케줄 CSV 대량 등록 + 업로드 UI."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_permission
from app.models.user import User
from app.services.bulk_upload_service import bulk_upload_service

router = APIRouter()


@router.get("/upload", response_class=HTMLResponse, include_in_schema=False)
async def upload_page():
    """Simple HTML upload page for bulk CSV import."""
    return UPLOAD_HTML


@router.post("/employees")
async def bulk_upload_employees(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("users:create")),
) -> dict:
    """CSV 파일로 직원 대량 등록 + 매장 배정.

    CSV columns: username, password, full_name, role, store_name, email, hourly_rate
    """
    if not file.filename:
        return {"error": "File required"}
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ("csv", "xlsx"):
        return {"error": "CSV or Excel (.xlsx) file required"}

    content = await file.read()
    result = await bulk_upload_service.process_employees(
        db, current_user.organization_id, content, caller=current_user,
        filename=file.filename,
    )
    return result


@router.post("/schedules")
async def bulk_upload_schedules(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("schedules:create")),
) -> dict:
    """Excel 파일로 스케줄 대량 등록. 시트별 매장 구분.

    Each sheet: Sheet name = Store name
    Row 1: Week Start | MM/DD/YYYY
    Row 2: Employee | Sun | Mon | ... | Sat
    Row 3+: schedule data
    """
    if not file.filename:
        return {"error": "File required"}
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ("csv", "xlsx"):
        return {"error": "CSV or Excel (.xlsx) file required"}

    content = await file.read()
    result = await bulk_upload_service.process_schedules(
        db, current_user.organization_id, content, created_by=current_user.id,
        filename=file.filename,
    )
    return result


# ── Inline HTML ──────────────────────────────────────────

UPLOAD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bulk Upload</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f5f5; padding: 40px 20px; color: #333; }
  .container { max-width: 640px; margin: 0 auto; }
  h1 { font-size: 24px; margin-bottom: 8px; }
  .subtitle { color: #666; margin-bottom: 32px; font-size: 14px; }
  .card { background: #fff; border-radius: 12px; padding: 24px;
          margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  .card h2 { font-size: 18px; margin-bottom: 4px; }
  .card p { color: #666; font-size: 13px; margin-bottom: 16px; }
  .file-input { display: block; width: 100%; padding: 12px;
                border: 2px dashed #ddd; border-radius: 8px; text-align: center;
                cursor: pointer; margin-bottom: 12px; font-size: 14px; }
  .file-input:hover { border-color: #999; }
  button { background: #2563eb; color: #fff; border: none; border-radius: 8px;
           padding: 12px 24px; font-size: 14px; cursor: pointer; width: 100%; }
  button:hover { background: #1d4ed8; }
  button:disabled { background: #94a3b8; cursor: not-allowed; }
  .result { margin-top: 16px; padding: 16px; border-radius: 8px;
            font-size: 13px; white-space: pre-wrap; display: none; }
  .result.success { background: #f0fdf4; border: 1px solid #86efac; }
  .result.error { background: #fef2f2; border: 1px solid #fca5a5; }
  .login-section { margin-bottom: 24px; }
  .login-section input { width: 100%; padding: 10px 12px; border: 1px solid #ddd;
                          border-radius: 8px; font-size: 14px; margin-bottom: 8px; }
  .login-section button { background: #059669; }
  .login-section button:hover { background: #047857; }
  .token-status { font-size: 12px; color: #666; margin-top: 4px; }
  .token-status.ok { color: #059669; }
  .token-status.fail { color: #dc2626; }
  .hidden { display: none; }
  .format-info { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;
                 padding: 12px; margin-bottom: 16px; font-size: 12px; color: #64748b; }
  .format-info code { background: #e2e8f0; padding: 1px 4px; border-radius: 3px; }
  .step-badge { display: inline-block; background: #2563eb; color: #fff;
                border-radius: 50%; width: 24px; height: 24px; text-align: center;
                line-height: 24px; font-size: 12px; font-weight: 600; margin-right: 8px; }
</style>
</head>
<body>
<div class="container">
  <h1>Bulk Upload</h1>
  <p class="subtitle">Upload CSV files to register employees and schedules</p>

  <!-- Login -->
  <div class="card login-section" id="loginSection">
    <h2>Login</h2>
    <p>Admin credentials required</p>
    <input type="text" id="username" placeholder="Username">
    <input type="password" id="password" placeholder="Password" onkeydown="if(event.key==='Enter')doLogin()">
    <button onclick="doLogin()">Login</button>
    <div id="tokenStatus" class="token-status"></div>
  </div>

  <!-- Upload section (hidden until login) -->
  <div id="uploadSection" class="hidden">

  <!-- Step 1: Employees -->
  <div class="card">
    <h2><span class="step-badge">1</span>Employee Registration</h2>
    <p>Register employees and assign to stores</p>
    <div class="format-info">
      CSV columns: <code>username</code>, <code>password</code>, <code>full_name</code>,
      <code>role</code>, <code>store_name</code>, <code>email</code>, <code>hourly_rate</code><br>
      Multiple stores: separate with comma (wrap in quotes)
    </div>
    <input type="file" class="file-input" id="empFile" accept=".csv,.xlsx">
    <button onclick="upload('employees', 'empFile', 'empResult')" id="empBtn">Upload Employees</button>
    <div id="empResult" class="result"></div>
  </div>

  <!-- Step 2: Schedules -->
  <div class="card">
    <h2><span class="step-badge">2</span>Schedule Registration</h2>
    <p>Upload weekly schedule (Excel, one sheet per store)</p>
    <div class="format-info">
      Sheet name = Store name<br>
      Row 1: <code>Week Start</code> | <code>MM/DD/YYYY</code><br>
      Row 2: <code>ID (Username)</code> | <code>Sun</code> | <code>Mon</code> | ... | <code>Sat</code><br>
      Cells: <code>9:00AM-6:00PM</code> or <code>9:00AM-6:00PM(12:00PM-1:00PM)</code>
    </div>
    <input type="file" class="file-input" id="schFile" accept=".csv,.xlsx">
    <button onclick="upload('schedules', 'schFile', 'schResult')" id="schBtn">Upload Schedules</button>
    <div id="schResult" class="result"></div>
  </div>

  </div><!-- /uploadSection -->
</div>

<script>
let TOKEN = sessionStorage.getItem('bulk_token') || '';

function showUpload() {
  document.getElementById('loginSection').classList.add('hidden');
  document.getElementById('uploadSection').classList.remove('hidden');
}

function showLogin() {
  document.getElementById('loginSection').classList.remove('hidden');
  document.getElementById('uploadSection').classList.add('hidden');
}

// If token exists, verify it's still valid
(async function checkToken() {
  if (!TOKEN) return;
  try {
    const resp = await fetch('/api/v1/admin/users?page=1&size=1', {
      headers: {'Authorization': 'Bearer ' + TOKEN},
    });
    if (resp.ok) {
      showUpload();
    } else {
      TOKEN = '';
      sessionStorage.removeItem('bulk_token');
    }
  } catch (e) {
    TOKEN = '';
    sessionStorage.removeItem('bulk_token');
  }
})();

async function doLogin() {
  const username = document.getElementById('username').value;
  const password = document.getElementById('password').value;
  const el = document.getElementById('tokenStatus');
  try {
    const resp = await fetch('/api/v1/admin/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, password}),
    });
    const data = await resp.json();
    if (data.access_token) {
      TOKEN = data.access_token;
      sessionStorage.setItem('bulk_token', TOKEN);
      showUpload();
    } else {
      el.textContent = 'Login failed: ' + (data.detail || JSON.stringify(data));
      el.className = 'token-status fail';
    }
  } catch (e) {
    el.textContent = 'Login error: ' + e.message;
    el.className = 'token-status fail';
  }
}

async function upload(type, fileId, resultId) {
  const fileInput = document.getElementById(fileId);
  const resultEl = document.getElementById(resultId);

  if (!TOKEN) {
    resultEl.style.display = 'block';
    resultEl.className = 'result error';
    resultEl.textContent = 'Please login first';
    return;
  }
  if (!fileInput.files.length) {
    resultEl.style.display = 'block';
    resultEl.className = 'result error';
    resultEl.textContent = 'Please select a CSV file';
    return;
  }

  const formData = new FormData();
  formData.append('file', fileInput.files[0]);

  resultEl.style.display = 'block';
  resultEl.className = 'result';
  resultEl.textContent = 'Uploading...';

  try {
    const resp = await fetch('/api/v1/admin/bulk/' + type, {
      method: 'POST',
      headers: {'Authorization': 'Bearer ' + TOKEN},
      body: formData,
    });
    const data = await resp.json();

    if (resp.ok && !data.error) {
      resultEl.className = 'result success';
      resultEl.textContent = JSON.stringify(data, null, 2);
    } else {
      resultEl.className = 'result error';
      resultEl.textContent = JSON.stringify(data, null, 2);
    }
  } catch (e) {
    resultEl.className = 'result error';
    resultEl.textContent = 'Error: ' + e.message;
  }
}
</script>
</body>
</html>
"""
