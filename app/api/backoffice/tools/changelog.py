"""Changelog 관리 — Backoffice 도구 (목록/작성/수정/발행/삭제).

org 권한 밖, 세션쿠키 인증만(get_current_admin). 글로벌 changelog.
P1: 마크다운 textarea 입력. WYSIWYG 에디터 임베드는 P2.
"""

import html as _html
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.backoffice import pages
from app.api.backoffice.deps import get_current_admin
from app.config import settings
from app.database import get_db
from app.models.changelog import CHANGELOG_CATEGORIES
from app.repositories.changelog_repository import changelog_repository
from app.schemas.changelog import ChangelogCreate, ChangelogUpdate
from app.services.changelog_service import (
    ChangelogNotFound,
    body_keys_to_urls,
    changelog_service,
)
from app.services.storage_service import storage_service

router: APIRouter = APIRouter(prefix="/tools/changelog", include_in_schema=False)

_ACTIVE = "/tools/changelog"


def _base() -> str:
    return "/" + settings.BACKOFFICE_PATH.strip("/")


def _esc(v: object) -> str:
    return _html.escape(str(v if v is not None else ""))


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


# --------------------------------------------------------------------------- #
# 폼 헬퍼
# --------------------------------------------------------------------------- #
def _category_select(selected: str | None) -> str:
    opts = "".join(
        f"<option value='{_esc(c)}'{' selected' if c == selected else ''}>{_esc(c)}</option>"
        for c in CHANGELOG_CATEGORIES
    )
    return f"<select class='sel' name='category' required>{opts}</select>"


def _parse_tags(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _form(base: str, action: str, post=None, submit_label: str = "Save") -> str:
    title = _esc(post.title) if post else ""
    summary = _esc(post.summary) if post else ""
    # 편집 로드 시 본문의 상대경로(key) 이미지를 전체 URL로 변환해 에디터가 미리보기하게 함.
    body = _esc(body_keys_to_urls(post.body)) if post else ""
    tags = _esc(", ".join(post.tags or [])) if post else ""
    cover = _esc(post.cover_image_key) if post else ""
    category = post.category if post else None
    upload_url = f"{base}/tools/changelog/upload-image"
    return (
        f"<form method='post' action='{action}' id='cl-form'>"
        "<label>Category</label>" + _category_select(category) +
        "<label style='margin-top:14px'>Title</label>"
        f"<input name='title' required value='{title}' maxlength='200'>"
        "<label>Summary (optional, one line)</label>"
        f"<input name='summary' value='{summary}' maxlength='500'>"
        "<label>Tags (comma-separated)</label>"
        f"<input name='tags' value='{tags}' placeholder='feature, bugfix'>"
        "<label>Cover image key (optional, relative path)</label>"
        f"<input name='cover_image_key' value='{cover}' maxlength='500'>"
        "<label>Body</label>"
        # WYSIWYG 에디터(Toast UI)가 마크다운을 생성해 제출 직전 hidden textarea에 채운다.
        # 서버 POST 핸들러는 변경 없이 name='body' 폼 필드를 그대로 받는다.
        "<div id='cl-editor' style='margin-bottom:16px'></div>"
        f"<textarea name='body' id='cl-body' style='display:none'>{body}</textarea>"
        f"<button type='submit' style='width:auto;padding:10px 22px'>{_esc(submit_label)}</button>"
        "</form>"
        "<link rel='stylesheet' href='https://uicdn.toast.com/editor/latest/toastui-editor.min.css'>"
        "<script src='https://uicdn.toast.com/editor/latest/toastui-editor-all.min.js'></script>"
        "<script>(function(){"
        "var ta=document.getElementById('cl-body');"
        "var initial=ta.value;"
        "var editor=new toastui.Editor({"
        "el:document.getElementById('cl-editor'),"
        "height:'480px',initialEditType:'wysiwyg',previewStyle:'vertical',"
        "initialValue:initial,"
        "hooks:{addImageBlobHook:function(blob,callback){"
        "var fd=new FormData();fd.append('file',blob,blob.name||'image.png');"
        f"fetch('{upload_url}',{{method:'POST',body:fd,credentials:'same-origin'}})"
        ".then(function(r){return r.json();})"
        ".then(function(j){if(j&&j.url){callback(j.url,'');}else{alert((j&&j.error)||'Image upload failed');}})"
        ".catch(function(){alert('Image upload failed');});"
        "return false;}}});"
        "document.getElementById('cl-form').addEventListener('submit',function(){"
        "ta.value=editor.getMarkdown();});"
        "})();</script>"
    )


# --------------------------------------------------------------------------- #
# 목록
# --------------------------------------------------------------------------- #
@router.get("", response_class=HTMLResponse)
async def list_page(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    admin = get_current_admin(request)
    base = _base()
    if not admin:
        return _redirect(f"{base}/login")

    posts = await changelog_repository.list_all(db)
    rows = ""
    for p in posts:
        status_pill = (
            "<span class='pill'>published</span>"
            if p.is_published
            else "<span class='pill pill-warn'>draft</span>"
        )
        when = p.published_at.strftime("%Y-%m-%d") if p.published_at else "—"
        rows += (
            f"<tr><td><a href='{base}/tools/changelog/{p.id}'>{_esc(p.title)}</a></td>"
            f"<td><span class='pill'>{_esc(p.category)}</span></td>"
            f"<td>{status_pill}</td><td class='hint'>{_esc(when)}</td></tr>"
        )
    if not rows:
        rows = "<tr><td colspan='4' class='empty'>No posts yet.</td></tr>"

    content = (
        "<div class='section'>"
        f"<a href='{base}/tools/changelog/new'><button type='button' "
        "style='width:auto;padding:9px 18px'>+ New post</button></a></div>"
        "<div class='section'><table class='dtable'>"
        "<thead><tr><th>Title</th><th>Category</th><th>Status</th><th>Published</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )
    return pages.shell(base, admin, _ACTIVE, "Changelog", content)


# --------------------------------------------------------------------------- #
# 작성
# --------------------------------------------------------------------------- #
@router.get("/new", response_class=HTMLResponse)
async def new_form(request: Request) -> HTMLResponse:
    admin = get_current_admin(request)
    base = _base()
    if not admin:
        return _redirect(f"{base}/login")
    content = f"<div class='section'>{_form(base, f'{base}/tools/changelog/new', submit_label='Create draft')}</div>"
    return pages.shell(base, admin, _ACTIVE, "New post", content)


@router.post("/new")
async def create(
    request: Request,
    db: AsyncSession = Depends(get_db),
    title: str = Form(...),
    category: str = Form(...),
    body: str = Form(...),
    summary: str = Form(""),
    tags: str = Form(""),
    cover_image_key: str = Form(""),
) -> RedirectResponse:
    admin = get_current_admin(request)
    base = _base()
    if not admin:
        return _redirect(f"{base}/login")
    payload = ChangelogCreate(
        title=title,
        category=category,  # type: ignore[arg-type]
        body=body,
        summary=summary or None,
        tags=_parse_tags(tags),
        cover_image_key=cover_image_key or None,
    )
    post = await changelog_service.create(db, payload)
    await db.commit()
    return _redirect(f"{base}/tools/changelog/{post.id}")


# --------------------------------------------------------------------------- #
# 이미지 업로드 (WYSIWYG 에디터 addImageBlobHook 용)
# --------------------------------------------------------------------------- #
# 주의: 이 라우트는 POST /{post_id} 보다 먼저 등록되어야 한다.
#       (UUID 경로 템플릿이 "upload-image" 를 가로채지 않도록)
@router.post("/upload-image")
async def upload_image(request: Request, file: UploadFile) -> JSONResponse:
    """에디터에서 붙여넣은/드롭한 이미지를 버킷에 저장하고 전체 URL 반환.

    DB에는 본문 마크다운 안에 상대경로(key)로 저장되지만, 에디터 라이브
    프리뷰를 위해 여기서는 resolve_url 결과(전체 URL)를 돌려준다.
    저장 시 body_urls_to_keys 가 다시 key 로 환원한다.
    """
    admin = get_current_admin(request)
    if not admin:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        return JSONResponse(
            {"error": "Only image files are allowed."}, status_code=400
        )

    data = await file.read()
    if not data:
        return JSONResponse({"error": "Empty file."}, status_code=400)

    key = storage_service.upload_bytes(
        data, file.filename or "image.png", "changelog", content_type
    )
    return JSONResponse({"url": storage_service.resolve_url(key)})


# --------------------------------------------------------------------------- #
# 수정 / 발행 / 삭제
# --------------------------------------------------------------------------- #
@router.get("/{post_id}", response_class=HTMLResponse)
async def edit_form(
    post_id: UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    admin = get_current_admin(request)
    base = _base()
    if not admin:
        return _redirect(f"{base}/login")
    post = await changelog_repository.get_by_id(db, post_id)
    if post is None:
        return pages.shell(base, admin, _ACTIVE, "Not found",
                           "<div class='empty'>Post not found.</div>")

    pub_label = "Unpublish" if post.is_published else "Publish"
    status_pill = (
        "<span class='pill'>published</span>" if post.is_published
        else "<span class='pill pill-warn'>draft</span>"
    )
    actions = (
        "<div class='confirm-bar'>"
        f"<form method='post' action='{base}/tools/changelog/{post.id}/publish'>"
        f"<button type='submit' style='width:auto;padding:8px 18px'>{pub_label}</button></form>"
        f"<form method='post' action='{base}/tools/changelog/{post.id}/delete' "
        "onsubmit=\"return confirm('Delete this post permanently?')\">"
        "<button type='submit' style='width:auto;padding:8px 18px;background:#c0392b'>Delete</button>"
        "</form>"
        f"<span class='hint'>Status: {status_pill} &nbsp; slug: <code>{_esc(post.slug)}</code></span>"
        "</div>"
    )
    content = (
        f"<div class='section'>{actions}</div>"
        f"<div class='section'>{_form(base, f'{base}/tools/changelog/{post.id}', post, 'Save changes')}</div>"
    )
    return pages.shell(base, admin, _ACTIVE, "Edit post", content)


@router.post("/{post_id}")
async def update(
    post_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    title: str = Form(...),
    category: str = Form(...),
    body: str = Form(...),
    summary: str = Form(""),
    tags: str = Form(""),
    cover_image_key: str = Form(""),
) -> RedirectResponse:
    admin = get_current_admin(request)
    base = _base()
    if not admin:
        return _redirect(f"{base}/login")
    payload = ChangelogUpdate(
        title=title,
        category=category,  # type: ignore[arg-type]
        body=body,
        summary=summary or None,
        tags=_parse_tags(tags),
        cover_image_key=cover_image_key or None,
    )
    try:
        await changelog_service.update(db, post_id, payload)
        await db.commit()
    except ChangelogNotFound:
        await db.rollback()
    return _redirect(f"{base}/tools/changelog/{post_id}")


@router.post("/{post_id}/publish")
async def toggle_publish(
    post_id: UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    admin = get_current_admin(request)
    base = _base()
    if not admin:
        return _redirect(f"{base}/login")
    post = await changelog_repository.get_by_id(db, post_id)
    if post is not None:
        await changelog_service.set_published(db, post_id, not post.is_published)
        await db.commit()
    return _redirect(f"{base}/tools/changelog/{post_id}")


@router.post("/{post_id}/delete")
async def delete(
    post_id: UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    admin = get_current_admin(request)
    base = _base()
    if not admin:
        return _redirect(f"{base}/login")
    try:
        await changelog_service.delete(db, post_id)
        await db.commit()
    except ChangelogNotFound:
        await db.rollback()
    return _redirect(f"{base}/tools/changelog")
