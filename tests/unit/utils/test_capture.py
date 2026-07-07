"""capture_time 정규화/강제 검증 단위 테스트.

normalize_photos: photos > photo_urls > photo_url 우선순위, legacy→unknown 정규화.
enforce_capture_time: required 게이트에 따른 거부/허용.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.schemas.common import PhotoMeta
from app.utils.capture import NormalizedPhoto, enforce_capture_time, normalize_photos
from app.utils.exceptions import CaptureTimeRequiredError

CT = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# normalize_photos — 우선순위
# ---------------------------------------------------------------------------


def test_photos_take_priority_over_legacy_fields():
    photos = [PhotoMeta(key="a.webp", capture_time=CT, capture_source="live")]
    result = normalize_photos(photos, photo_urls=["legacy.jpg"], photo_url="single.jpg")
    assert len(result) == 1
    assert result[0].key == "a.webp"
    assert result[0].capture_time == CT
    assert result[0].capture_source == "live"


def test_photo_urls_used_when_no_photos():
    result = normalize_photos(None, photo_urls=["x.jpg", "y.jpg"], photo_url="single.jpg")
    assert [p.key for p in result] == ["x.jpg", "y.jpg"]
    # legacy 경로 → 출처/시각 미상
    assert all(p.capture_time is None for p in result)
    assert all(p.capture_source == "unknown" for p in result)


def test_single_photo_url_used_as_last_resort():
    result = normalize_photos(None, photo_urls=None, photo_url="single.jpg")
    assert len(result) == 1
    assert result[0].key == "single.jpg"
    assert result[0].capture_source == "unknown"


def test_empty_when_nothing_provided():
    assert normalize_photos(None, None, None) == []
    assert normalize_photos([], [], None) == []


# ---------------------------------------------------------------------------
# normalize_photos — capture_source 정규화 (보수적)
# ---------------------------------------------------------------------------


def test_gallery_source_preserved():
    photos = [PhotoMeta(key="g.webp", capture_time=CT, capture_source="gallery")]
    result = normalize_photos(photos, None, None)
    assert result[0].capture_source == "gallery"


def test_invalid_source_normalized_to_unknown():
    photos = [PhotoMeta(key="x.webp", capture_time=CT, capture_source="bogus")]
    result = normalize_photos(photos, None, None)
    assert result[0].capture_source == "unknown"


def test_missing_source_is_unknown_even_with_capture_time():
    # capture_time 만 있다고 live 로 추정하지 않는다
    photos = [PhotoMeta(key="x.webp", capture_time=CT, capture_source=None)]
    result = normalize_photos(photos, None, None)
    assert result[0].capture_source == "unknown"
    assert result[0].capture_time == CT


# ---------------------------------------------------------------------------
# enforce_capture_time — 게이트
# ---------------------------------------------------------------------------


def test_enforce_noop_when_not_required():
    photos = [NormalizedPhoto(key="a", capture_time=None, capture_source="unknown")]
    # 거부하지 않음 (받되 플래그)
    enforce_capture_time(photos, required=False)


def test_enforce_passes_when_all_have_capture_time():
    photos = [NormalizedPhoto(key="a", capture_time=CT, capture_source="live")]
    enforce_capture_time(photos, required=True)


def test_enforce_rejects_when_any_missing_capture_time():
    photos = [
        NormalizedPhoto(key="a", capture_time=CT, capture_source="live"),
        NormalizedPhoto(key="b", capture_time=None, capture_source="unknown"),
    ]
    with pytest.raises(CaptureTimeRequiredError):
        enforce_capture_time(photos, required=True)


def test_enforce_error_carries_structured_detail():
    with pytest.raises(CaptureTimeRequiredError) as exc:
        enforce_capture_time(
            [NormalizedPhoto(key="a", capture_time=None, capture_source="unknown")],
            required=True,
        )
    detail = exc.value.detail
    assert detail["code"] == "CAPTURE_TIME_REQUIRED"
    assert "message" in detail
    assert "hint" in detail
    assert exc.value.status_code == 422
