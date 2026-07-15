"""Unit — 이미지 인코딩 유틸 (Phase 2 처리).

대상: app/utils/image.py
  - profile_for_folder / 표준 프로파일 5종
  - to_webp_key / thumb_key (read-time 파생 컨벤션)
  - sniff_image_format (이미지 vs 비이미지)
  - render_derivatives (full/thumb 생성, square crop, pass-through 분기)
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from app.utils import image as im


def _jpeg(w: int, h: int, color=(123, 45, 67)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


def _png_rgba(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (10, 20, 30, 128)).save(buf, "PNG")
    return buf.getvalue()


def _heic(w: int, h: int, color=(200, 100, 50)) -> bytes:
    # pillow-heif 가 등록돼 있으면 Pillow 로 HEIF 저장 가능(테스트 픽스처).
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "HEIF")
    return buf.getvalue()


def _dims(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.size


def _fmt(data: bytes) -> str | None:
    with Image.open(io.BytesIO(data)) as img:
        return img.format


# ── 프로파일 매핑 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "folder,expected",
    [
        ("completions", "VERIFY_PHOTO"),
        ("reviews", "VERIFY_PHOTO"),
        ("chat", "VERIFY_PHOTO"),
        ("tasks", "VERIFY_PHOTO"),
        ("issues", "VERIFY_PHOTO"),
        ("applicant_attachments", "VERIFY_PHOTO"),
        ("products", "PRODUCT_SQUARE"),
        ("store_covers", "COVER"),
        ("profiles", "AVATAR"),
        ("warnings", "DOC_PASSTHROUGH"),
        ("notices", "DOC_PASSTHROUGH"),
        ("unknown_folder", "VERIFY_PHOTO"),  # 기본값
    ],
)
def test_profile_for_folder(folder: str, expected: str) -> None:
    assert im.profile_for_folder(folder).name == expected


def test_passthrough_profile_does_not_encode() -> None:
    assert im.DOC_PASSTHROUGH.encode is False


# ── key 컨벤션 ────────────────────────────────────────────────


def test_to_webp_key_replaces_extension() -> None:
    assert im.to_webp_key("completions/2026/06/22/abc.jpg") == "completions/2026/06/22/abc.webp"
    assert im.to_webp_key("a/b/c.JPEG") == "a/b/c.webp"
    assert im.to_webp_key("noext") == "noext.webp"


def test_thumb_key_from_webp() -> None:
    assert im.thumb_key("x/y/abc.webp") == "x/y/abc.thumb.webp"


def test_thumb_key_passthrough_non_webp() -> None:
    # 비-webp(레거시/비이미지)는 그대로 — 썸네일 없음
    assert im.thumb_key("x/y/abc.jpg") == "x/y/abc.jpg"


# ── sniff ─────────────────────────────────────────────────────


def test_sniff_detects_image() -> None:
    assert im.sniff_image_format(_jpeg(10, 10)) == "jpeg"
    assert im.sniff_image_format(_png_rgba(10, 10)) == "png"


def test_sniff_rejects_non_image() -> None:
    assert im.sniff_image_format(b"%PDF-1.4 not an image") is None
    assert im.sniff_image_format(b"\x00\x01\x02random") is None
    assert im.sniff_image_format(b"") is None


# ── render_derivatives ────────────────────────────────────────


def test_verify_photo_full_and_thumb() -> None:
    d = im.render_derivatives(_jpeg(3000, 2000), im.VERIFY_PHOTO)
    assert set(d) == {"full", "thumb"}
    assert _fmt(d["full"]) == "WEBP"
    # 긴 변 2048 로 축소, 비율 보존
    assert max(_dims(d["full"])) == 2048
    assert _dims(d["full"]) == (2048, 1365)
    assert max(_dims(d["thumb"])) == 320


def test_verify_photo_no_upscale_small_image() -> None:
    # 원본이 작으면 확대하지 않음
    d = im.render_derivatives(_jpeg(200, 150), im.VERIFY_PHOTO)
    assert _dims(d["full"]) == (200, 150)


def test_product_square_single_square_thumb() -> None:
    d = im.render_derivatives(_jpeg(3000, 2000), im.PRODUCT_SQUARE)
    assert set(d) == {"thumb"}  # full 없음
    assert _dims(d["thumb"]) == (320, 320)  # center-crop 정사각


def test_avatar_square_96() -> None:
    d = im.render_derivatives(_jpeg(1000, 800), im.AVATAR)
    assert set(d) == {"thumb"}
    assert _dims(d["thumb"]) == (96, 96)


def test_cover_full_and_thumb() -> None:
    d = im.render_derivatives(_jpeg(4000, 1000), im.COVER)
    assert set(d) == {"full", "thumb"}
    assert max(_dims(d["full"])) == 2048


def test_rgba_png_encodes_to_webp() -> None:
    # 투명도 PNG 도 WebP 로 인코딩 (RGBA 보존)
    d = im.render_derivatives(_png_rgba(800, 600), im.VERIFY_PHOTO)
    assert _fmt(d["full"]) == "WEBP"


def test_non_image_passthrough_empty() -> None:
    assert im.render_derivatives(b"%PDF-1.4 doc", im.VERIFY_PHOTO) == {}
    assert im.render_derivatives(b"\x00random", im.VERIFY_PHOTO) == {}


# ── HEIC (아이폰 사진) ─────────────────────────────────────────


@pytest.mark.skipif(not im._HEIF_ENABLED, reason="pillow-heif 미설치")
def test_heic_sniffed_as_image() -> None:
    # HEIC 바이트가 이제 이미지로 인식된다(pass-through 대상 아님).
    assert im.sniff_image_format(_heic(10, 10)) is not None


@pytest.mark.skipif(not im._HEIF_ENABLED, reason="pillow-heif 미설치")
def test_heic_transcoded_to_webp() -> None:
    # HEIC → full/thumb WebP 로 변환(브라우저/콘솔에서 표시 가능).
    d = im.render_derivatives(_heic(3000, 2000), im.VERIFY_PHOTO)
    assert set(d) == {"full", "thumb"}
    assert _fmt(d["full"]) == "WEBP"
    assert max(_dims(d["full"])) == 2048


def test_doc_profile_skips_even_image() -> None:
    # DOC_PASSTHROUGH 는 이미지여도 인코딩 안 함
    assert im.render_derivatives(_jpeg(1000, 1000), im.DOC_PASSTHROUGH) == {}
