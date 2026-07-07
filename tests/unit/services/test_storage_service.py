"""Unit — storage_service 고수준 단일 진입점 + 중앙 검증 (Phase 1 통합).

대상: app/services/storage_service.py
  - validate_folder / validate_upload_key (중앙 라우팅·키 안전성)
  - presign_upload (폴더 allowlist 검증)
  - put_bytes (랜덤키=폴더검증 / 고정키=키검증 분기)
  - receive_upload (raw PUT 키 안전성)
  - put_finalized (temp → 최종)

저장(write) 진입점이 한 표면으로 모였고, 폴더·키 검증이 한 곳에서 강제됨을 보장한다.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.services import storage_service as ss
from app.services.storage_service import (
    ALLOWED_FOLDERS,
    InvalidStorageFolder,
    UnsafeStorageKey,
    storage_service,
    validate_folder,
    validate_upload_key,
)


# ── validate_folder ───────────────────────────────────────────


@pytest.mark.parametrize("folder", sorted(ALLOWED_FOLDERS))
def test_validate_folder_allows_registered(folder: str) -> None:
    assert validate_folder(folder) == folder


@pytest.mark.parametrize("folder", ["", "secrets", "etc", "../escape", "Tasks"])
def test_validate_folder_rejects_unregistered(folder: str) -> None:
    with pytest.raises(InvalidStorageFolder):
        validate_folder(folder)


def test_allowed_folders_cover_known_clients() -> None:
    """클라이언트(app/console)가 실제 보내는 folder 가 모두 등록돼 있어야 한다."""
    client_folders = {"completions", "reviews", "tasks", "chat", "products"}
    assert client_folders <= ALLOWED_FOLDERS


# ── validate_upload_key ───────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "temp/completions/2026/06/22/abc.jpg",
        "signatures/users/42/x.png",
        "forms/4070/7/9.pdf",
    ],
)
def test_validate_upload_key_allows_safe_prefixes(key: str) -> None:
    assert validate_upload_key(key) == key


@pytest.mark.parametrize(
    "key",
    [
        "",
        "../etc/passwd",
        "temp/../../etc/passwd",
        "/absolute/path.jpg",
        "completions/2026/06/22/x.jpg",   # finalize 후 경로는 raw PUT 대상 아님
        "evil/key.jpg",
        "temp\\windows\\x.jpg",
    ],
)
def test_validate_upload_key_rejects_unsafe(key: str) -> None:
    with pytest.raises(UnsafeStorageKey):
        validate_upload_key(key)


# ── presign_upload ────────────────────────────────────────────


def test_presign_upload_rejects_bad_folder() -> None:
    with pytest.raises(InvalidStorageFolder):
        storage_service.presign_upload(
            folder="not_allowed", filename="a.jpg", content_type="image/jpeg"
        )


def test_presign_upload_good_folder_returns_temp_key() -> None:
    result = storage_service.presign_upload(
        folder="completions", filename="a.jpg", content_type="image/jpeg"
    )
    assert set(result) >= {"upload_url", "file_url", "key"}
    assert result["key"].startswith("temp/")
    assert result["key"].endswith(".jpg")


# ── put_bytes 분기 ────────────────────────────────────────────


def test_put_bytes_random_key_rejects_bad_folder() -> None:
    with pytest.raises(InvalidStorageFolder):
        storage_service.put_bytes(b"x", folder="nope", filename="a.jpg")


def test_put_bytes_fixed_key_rejects_unsafe_key() -> None:
    with pytest.raises(UnsafeStorageKey):
        storage_service.put_bytes(b"x", key="../escape.png")


# ── 디스크 쓰기 경로 (로컬 모드에서만) ─────────────────────────

requires_local = pytest.mark.skipif(
    not storage_service.is_local, reason="local storage mode 전용"
)


@requires_local
def test_receive_upload_writes_temp_key_and_rejects_traversal() -> None:
    key = f"temp/completions/test/{uuid.uuid4().hex}.bin"
    path = ss.BUCKET_DIR / key
    try:
        storage_service.receive_upload(key, b"hello")
        assert path.exists()
        assert path.read_bytes() == b"hello"
    finally:
        if path.exists():
            path.unlink()

    with pytest.raises(UnsafeStorageKey):
        storage_service.receive_upload("../../etc/evil.bin", b"x")


@requires_local
def test_put_bytes_fixed_key_writes_to_that_key() -> None:
    key = f"forms/4070/test/{uuid.uuid4().hex}.pdf"
    path: Path = ss.BUCKET_DIR / key
    try:
        returned = storage_service.put_bytes(b"%PDF", key=key, content_type="application/pdf")
        assert returned == key
        assert path.exists()
    finally:
        if path.exists():
            path.unlink()


@requires_local
def test_put_finalized_moves_temp_to_final() -> None:
    name = uuid.uuid4().hex
    temp_key = f"temp/reviews/test/{name}.bin"
    final_key = f"reviews/test/{name}.bin"
    temp_path = ss.BUCKET_DIR / temp_key
    final_path = ss.BUCKET_DIR / final_key
    try:
        storage_service.receive_upload(temp_key, b"data")
        returned = storage_service.put_finalized(temp_key)
        assert returned == final_key
        assert final_path.exists()
        assert not temp_path.exists()
    finally:
        for p in (temp_path, final_path):
            if p.exists():
                p.unlink()


# ── Phase 2: 인코딩 통합 (로컬 모드) ───────────────────────────


def _jpeg(w: int, h: int) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 90, 160)).save(buf, "JPEG")
    return buf.getvalue()


@requires_local
def test_put_finalized_image_encodes_webp_and_thumb() -> None:
    name = uuid.uuid4().hex
    temp_key = f"temp/completions/test/{name}.jpg"
    cleanup = [ss.BUCKET_DIR / temp_key]
    try:
        storage_service.receive_upload(temp_key, _jpeg(2500, 1600))
        final = storage_service.put_finalized(temp_key)
        # 이미지 → webp key 반환
        assert final.endswith(".webp")
        full_path = ss.BUCKET_DIR / final
        thumb_path = ss.BUCKET_DIR / ss.thumb_key(final)
        cleanup += [full_path, thumb_path]
        assert full_path.exists()
        assert thumb_path.exists()
        # 원본 temp 폐기(원본폐기 c)
        assert not (ss.BUCKET_DIR / temp_key).exists()
    finally:
        for p in cleanup:
            if p.exists():
                p.unlink()


@requires_local
def test_put_finalized_pdf_passthrough_no_encode() -> None:
    name = uuid.uuid4().hex
    temp_key = f"temp/warnings/test/{name}.pdf"
    final_key = f"warnings/test/{name}.pdf"
    cleanup = [ss.BUCKET_DIR / temp_key, ss.BUCKET_DIR / final_key]
    try:
        storage_service.receive_upload(temp_key, b"%PDF-1.4 hello")
        final = storage_service.put_finalized(temp_key)
        # 비이미지 → 확장자 보존, webp 아님
        assert final == final_key
        assert (ss.BUCKET_DIR / final_key).exists()
    finally:
        for p in cleanup:
            if p.exists():
                p.unlink()


@requires_local
def test_put_bytes_folder_image_encodes_webp() -> None:
    final = None
    cleanup = []
    try:
        final = storage_service.put_bytes(
            _jpeg(3000, 2000), folder="store_covers", filename="cover.jpg"
        )
        assert final.endswith(".webp")
        full_path = ss.BUCKET_DIR / final
        thumb_path = ss.BUCKET_DIR / ss.thumb_key(final)
        cleanup += [full_path, thumb_path]
        assert full_path.exists()
        assert thumb_path.exists()  # COVER = full + thumb
    finally:
        for p in cleanup:
            if p.exists():
                p.unlink()


@requires_local
def test_resolve_url_thumb_falls_back_to_base_when_no_thumb() -> None:
    # 단일파생(products=PRODUCT_SQUARE) → thumb 파일 별도 없음 → base 로 폴백
    final = None
    cleanup = []
    try:
        final = storage_service.put_bytes(
            _jpeg(800, 600), folder="products", filename="p.jpg"
        )
        cleanup.append(ss.BUCKET_DIR / final)
        # PRODUCT_SQUARE 는 단일 파생을 base key 에 저장(별도 thumb 파일 없음)
        assert not (ss.BUCKET_DIR / ss.thumb_key(final)).exists()
        thumb_url = storage_service.resolve_url(final, variant="thumb")
        full_url = storage_service.resolve_url(final, variant="full")
        # 썸네일 부재 → base(full) URL 로 폴백
        assert thumb_url == full_url
    finally:
        for p in cleanup:
            if p.exists():
                p.unlink()


@requires_local
def test_resolve_url_thumb_returns_thumb_when_present() -> None:
    final = None
    cleanup = []
    try:
        final = storage_service.put_bytes(
            _jpeg(2500, 1600), folder="reviews", filename="r.jpg"
        )
        full_path = ss.BUCKET_DIR / final
        thumb_path = ss.BUCKET_DIR / ss.thumb_key(final)
        cleanup += [full_path, thumb_path]
        assert thumb_path.exists()
        thumb_url = storage_service.resolve_url(final, variant="thumb")
        assert thumb_url is not None
        assert thumb_url.endswith(ss.thumb_key(final))
    finally:
        for p in cleanup:
            if p.exists():
                p.unlink()
