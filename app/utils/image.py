"""이미지 인코딩 유틸 — WebP 변환 + 썸네일 + 표준 프로파일.

Phase 2 (이미지 파이프라인): Phase 1에서 만든 단일 저장 진입점 위에 인코딩을
얹는다. 도메인 코드는 이 모듈을 직접 부르지 않고 storage_service 가 호출한다.

설계 결정 (docs/99_inbox/2026-06-22-이미지-처리-파이프라인-설계 / 구현-Phase):
  - 인코딩 = WebP. optimized full long-edge 2048 q80, thumb 320 q75. AVIF 안 함.
  - 표준 프로파일 5종 + 폴더→프로파일 매핑.
  - 혼재 도메인(tasks/issues/chat/applicant_attachments)은 폴더 일괄이 아니라
    **실제 바이트가 디코딩 가능한 이미지인지**로 결정한다. 이미지가 아니면
    (PDF·동영상·서명 PNG가 아닌 임의 바이너리) 인코딩을 건너뛰고 원본 보존.
  - 엔진은 Pillow (이미 의존성). 설계 문서는 pyvips를 적었으나 pyvips는 prod EC2
    에 libvips 시스템 패키지 설치가 필요하고, 동기 단건 인코딩엔 Pillow로 충분.
    대량 백필(Phase 3)이 속도를 요구하면 그때 교체 검토.

HEIC: pillow-heif 플러그인을 등록하면 Pillow 가 HEIC/HEIF 를 열 수 있다. 아래에서
import 시 1회 register_heif_opener() 하므로, HEIC(아이폰 사진)도 다른 포맷과
동일하게 sniff/encode 경로를 타 WebP 로 변환된다. 플러그인이 없으면(구 환경)
조용히 넘어가고 기존처럼 HEIC pass-through(원본 보존) — 기존보다 나빠지지 않음.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# HEIC/HEIF 디코드 활성화 — Pillow 에 HEIF opener 를 전역 등록(import 1회).
# sniff_image_format / _encode_one 의 Image.open 이 이후 HEIC 를 인식한다.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    _HEIF_ENABLED = True
except Exception:  # pragma: no cover - 플러그인 미설치 환경 폴백
    _HEIF_ENABLED = False
    logger.warning("pillow-heif 미설치 — HEIC 는 pass-through(원본 보존)로 처리됨")

WEBP_EXT = ".webp"
THUMB_SUFFIX = ".thumb.webp"


@dataclass(frozen=True)
class ImageProfile:
    """도메인별 파생(derivative) 생성 규칙.

    full/thumb 의 max_edge 가 None 이면 해당 티어를 만들지 않는다.
    square=True 면 center-crop 정사각(그리드/아바타용), False 면 비율 보존.
    encode=False 면 비이미지 전용(인코딩 자체를 건너뜀).
    """

    name: str
    encode: bool = True
    full_max_edge: int | None = 2048
    full_quality: int = 80
    thumb_max_edge: int | None = 320
    thumb_quality: int = 75
    square: bool = False


# ── 표준 프로파일 5종 ─────────────────────────────────────────
VERIFY_PHOTO = ImageProfile("VERIFY_PHOTO")  # full 2048 + thumb 320, 비율보존
PRODUCT_SQUARE = ImageProfile(
    "PRODUCT_SQUARE", full_max_edge=None, thumb_max_edge=320, square=True
)
COVER = ImageProfile("COVER")  # = VERIFY_PHOTO 와 동일 세트(full+thumb, 비율보존)
AVATAR = ImageProfile(
    "AVATAR", full_max_edge=None, thumb_max_edge=96, thumb_quality=75, square=True
)
DOC_PASSTHROUGH = ImageProfile("DOC_PASSTHROUGH", encode=False)


# 논리 폴더 → 프로파일. 미등록 폴더는 DEFAULT_PROFILE(이미지면 full+thumb).
# 혼재 폴더(tasks/issues/chat/applicant_attachments)도 VERIFY_PHOTO 로 두되,
# 실제 인코딩 여부는 바이트가 이미지인지로 갈린다(비이미지는 자동 pass-through).
FOLDER_PROFILES: dict[str, ImageProfile] = {
    "completions": VERIFY_PHOTO,
    "reviews": VERIFY_PHOTO,
    "chat": VERIFY_PHOTO,
    "tasks": VERIFY_PHOTO,
    "issues": VERIFY_PHOTO,
    "applicant_attachments": VERIFY_PHOTO,
    "products": PRODUCT_SQUARE,
    "store_covers": COVER,
    "profiles": AVATAR,
    # 항상 비이미지(서명 PDF·공지) — 이미지가 와도 재인코딩하지 않음
    "warnings": DOC_PASSTHROUGH,
    "notices": DOC_PASSTHROUGH,
}
DEFAULT_PROFILE = VERIFY_PHOTO


def profile_for_folder(folder: str) -> ImageProfile:
    """논리 폴더 이름 → 프로파일. 미등록은 기본(VERIFY_PHOTO)."""
    return FOLDER_PROFILES.get(folder, DEFAULT_PROFILE)


# ── key 네이밍 컨벤션 (DB엔 base key만, thumb는 read-time 파생) ──


def to_webp_key(key: str) -> str:
    """확장자를 .webp 로 교체한 base key. (이미지 인코딩 후 최종 key)"""
    stem = key.rsplit(".", 1)[0] if "." in key else key
    return stem + WEBP_EXT


def thumb_key(webp_key: str) -> str:
    """base webp key → thumb key. 이미지당 thumb 1장이라 size 토큰 없이 단일.

    abc.webp → abc.thumb.webp. .webp 가 아니면(비이미지) None 의미로 그대로 반환.
    """
    if not webp_key.endswith(WEBP_EXT):
        return webp_key
    return webp_key[: -len(WEBP_EXT)] + THUMB_SUFFIX


# ── 인코딩 ────────────────────────────────────────────────────


def sniff_image_format(data: bytes) -> str | None:
    """바이트가 Pillow 로 열리는 이미지면 포맷명(소문자), 아니면 None.

    PDF·동영상·임의 바이너리는 None → 호출부에서 pass-through.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            return fmt.lower() if fmt else None
    except Exception:
        return None


def _encode_one(data: bytes, *, max_edge: int, quality: int, square: bool) -> bytes:
    """단일 derivative 인코딩. EXIF 회전 보정 → (square면 center-crop) →
    축소(확대 안 함) → WebP 저장. 실패 시 예외 전파(호출부가 pass-through 결정)."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)  # 휴대폰 사진 회전 보정

        # WebP 모드 정규화: 투명도 있으면 RGBA, 아니면 RGB
        if img.mode in ("RGBA", "LA") or (
            img.mode == "P" and "transparency" in img.info
        ):
            img = img.convert("RGBA")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        resample = Image.Resampling.LANCZOS
        if square:
            # center-crop 정사각 후 edge 로 리사이즈 (그리드/아바타)
            img = ImageOps.fit(img, (max_edge, max_edge), method=resample)
        else:
            # 비율 보존, 긴 변 max_edge 로 축소(원본이 더 작으면 그대로 — 확대 안 함)
            img.thumbnail((max_edge, max_edge), resample)

        out = io.BytesIO()
        img.save(out, format="WEBP", quality=quality, method=6)
        return out.getvalue()


def render_derivatives(data: bytes, profile: ImageProfile) -> dict[str, bytes]:
    """프로파일에 따라 full/thumb WebP 바이트를 생성.

    반환: {"full": bytes, "thumb": bytes} (해당 티어가 있을 때만 키 포함).
    인코딩 대상이 아니거나(비이미지/DOC_PASSTHROUGH) 디코딩 실패 시 **빈 dict**
    → 호출부는 원본을 그대로 저장(pass-through)한다.
    """
    if not profile.encode:
        return {}
    if sniff_image_format(data) is None:
        return {}

    result: dict[str, bytes] = {}
    try:
        if profile.full_max_edge is not None:
            result["full"] = _encode_one(
                data,
                max_edge=profile.full_max_edge,
                quality=profile.full_quality,
                square=profile.square,
            )
        if profile.thumb_max_edge is not None:
            result["thumb"] = _encode_one(
                data,
                max_edge=profile.thumb_max_edge,
                quality=profile.thumb_quality,
                square=profile.square,
            )
    except Exception:
        # 인코딩 도중 실패 → 안전하게 pass-through (원본 보존)
        logger.warning("이미지 인코딩 실패, 원본 보존(pass-through)", exc_info=True)
        return {}
    return result
