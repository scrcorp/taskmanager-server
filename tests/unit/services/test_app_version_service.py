"""Unit tests — app_version_service module.

[작성됨]
- _version_sort_key (semver+build 비교)
- _extract_version_from_key (파일명/path 우선순위)
- get_latest_attendance_from_storage (local mode: tmp dir 활용)
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.app_version_service import (
    _extract_version_from_key,
    _version_sort_key,
    app_version_service,
)


# ── _version_sort_key ──────────────────────────────────────────


def test_version_sort_key_semver_only() -> None:
    assert _version_sort_key("1.0.9") == (1, 0, 9, 0)


def test_version_sort_key_with_build() -> None:
    assert _version_sort_key("1.0.9+27") == (1, 0, 9, 27)


def test_version_sort_key_invalid_returns_zeros() -> None:
    assert _version_sort_key("abc") == (0, 0, 0, 0)


def test_version_sort_key_orders_correctly() -> None:
    versions = ["1.0.10+30", "1.0.9+27", "1.0.9+28", "2.0.0+1"]
    sorted_versions = sorted(versions, key=_version_sort_key, reverse=True)
    assert sorted_versions[0] == "2.0.0+1"
    assert sorted_versions[1] == "1.0.10+30"  # 1.0.10 > 1.0.9
    assert sorted_versions[2] == "1.0.9+28"   # build 28 > 27
    assert sorted_versions[3] == "1.0.9+27"


# ── _extract_version_from_key ──────────────────────────────────


def test_extract_version_from_filename_with_build() -> None:
    """파일명에 풀버전(`+build`) 있으면 그게 우선."""
    key = "app-releases/attendance/v1.0.9/htma_1.0.9+27.apk"
    assert _extract_version_from_key(key) == "1.0.9+27"


def test_extract_version_from_path_when_no_build_in_filename() -> None:
    """파일명에 버전 없으면 path 의 v{X.Y.Z} fallback."""
    key = "app-releases/attendance/v1.0.7/htma.apk"
    assert _extract_version_from_key(key) == "1.0.7"


def test_extract_version_returns_none_for_unrelated_key() -> None:
    assert _extract_version_from_key("app-releases/other/file.apk") is None


# ── get_latest_attendance_from_storage (local mode) ───────────


@pytest.fixture
def tmp_bucket(tmp_path: Path) -> Path:
    """tmp 디렉토리에 LOCAL_BUCKET_DIR 흉내. attendance APK 파일들 미리 셋업."""
    base = tmp_path / "app-releases" / "attendance"
    (base / "v1.0.7").mkdir(parents=True)
    (base / "v1.0.7" / "htma_1.0.7+20.apk").write_text("v1.0.7")
    (base / "v1.0.8").mkdir(parents=True)
    (base / "v1.0.8" / "htma_1.0.8+26.apk").write_text("v1.0.8")
    (base / "v1.0.9").mkdir(parents=True)
    (base / "v1.0.9" / "htma_1.0.9+27.apk").write_text("v1.0.9")
    # test APK (제외 대상)
    (base / "v1.0.9" / "htma_test_1.0.9+27.apk").write_text("test")
    return tmp_path


def test_get_latest_picks_highest_semver_from_local(tmp_bucket: Path) -> None:
    """여러 버전 중 가장 최신 (semver+build) 선택."""
    with patch.object(app_version_service.__class__.__bases__[0] if False else type(app_version_service), "__init__", lambda self: None):
        pass  # placeholder

    from app.config import settings as app_settings
    original = app_settings.LOCAL_BUCKET_DIR
    app_settings.LOCAL_BUCKET_DIR = str(tmp_bucket)
    try:
        # storage_service.is_local 은 STORAGE_MODE 로 결정. local 기본값.
        result = app_version_service.get_latest_attendance_from_storage()
        assert result is not None
        assert result["version"] == "1.0.9+27"
        assert "htma_1.0.9+27.apk" in result["key"]
    finally:
        app_settings.LOCAL_BUCKET_DIR = original


def test_get_latest_returns_none_when_empty(tmp_path: Path) -> None:
    """attendance APK 없으면 None."""
    from app.config import settings as app_settings
    original = app_settings.LOCAL_BUCKET_DIR
    app_settings.LOCAL_BUCKET_DIR = str(tmp_path)  # 빈 dir
    try:
        result = app_version_service.get_latest_attendance_from_storage()
        assert result is None
    finally:
        app_settings.LOCAL_BUCKET_DIR = original


def test_get_latest_excludes_test_apk(tmp_bucket: Path) -> None:
    """파일명에 'test' 들어간 APK 는 무시."""
    # tmp_bucket 에 v1.0.10 test 만 추가 (prod 는 v1.0.9 가 max 상태)
    base = tmp_bucket / "app-releases" / "attendance" / "v1.0.10"
    base.mkdir(parents=True)
    (base / "htma_test_1.0.10+30.apk").write_text("test")  # 제외돼야 함

    from app.config import settings as app_settings
    original = app_settings.LOCAL_BUCKET_DIR
    app_settings.LOCAL_BUCKET_DIR = str(tmp_bucket)
    try:
        result = app_version_service.get_latest_attendance_from_storage()
        # test 파일 무시 → 1.0.9+27 가 여전히 max
        assert result is not None
        assert result["version"] == "1.0.9+27"
    finally:
        app_settings.LOCAL_BUCKET_DIR = original
