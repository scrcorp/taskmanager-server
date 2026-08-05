"""HTMA 릴리스 채널명 결정 규칙.

이 값이 `release-attendance.sh` 가 등록하는 채널명과 어긋나면 앱이 최신 APK 를
찾지 못하고 **조용히 "이미 최신"으로 표시된다**. prod/staging 에서 실제로 그렇게
깨져 있었고(둘 다 attendance_local 로 조회), 에러가 안 나서 한 달 넘게 방치됐다.
그래서 규칙을 테스트로 고정한다.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.app_version_service import app_version_service


@pytest.fixture
def restore_env():
    """settings 값 원복 — 다른 테스트에 새지 않게."""
    original = (settings.APP_ENV, settings.HTMA_TYPE)
    yield
    settings.APP_ENV, settings.HTMA_TYPE = original


@pytest.mark.parametrize(
    "htma_type,expected",
    [
        ("production", "attendance_production"),
        ("staging", "attendance_staging"),
        ("local", "attendance_local"),
    ],
)
def test_channel_follows_htma_type(restore_env, htma_type: str, expected: str) -> None:
    """채널은 HTMA_TYPE 을 따른다 — 릴리스 스크립트의 채널명과 같은 형태."""
    settings.APP_ENV = "local"
    settings.HTMA_TYPE = htma_type
    assert app_version_service.attendance_channel() == expected


def test_htma_type_wins_over_app_env(restore_env) -> None:
    """서버 배포 환경과 앱 채널은 별개 축 — HTMA_TYPE 이 우선한다."""
    settings.APP_ENV = "production"
    settings.HTMA_TYPE = "staging"
    assert app_version_service.attendance_channel() == "attendance_staging"


def test_falls_back_to_app_env_when_unset(restore_env) -> None:
    """HTMA_TYPE 미설정이면 APP_ENV 로 폴백 — 변수 도입 이전 .env 하위호환."""
    settings.APP_ENV = "production"
    settings.HTMA_TYPE = ""
    assert app_version_service.attendance_channel() == "attendance_production"


def test_release_script_channels_are_covered() -> None:
    """release-attendance.sh 가 쓰는 채널명 2종이 이 규칙으로 재현되는지.

    스크립트가 하드코딩한 문자열과 서버 계산식이 갈라지면 다시 같은 사고가 난다.
    """
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[3] / ".." / ".." / ".." / ".." / "scripts"
        / "release-attendance.sh"
    )
    if not script.exists():
        pytest.skip("release-attendance.sh 경로를 찾지 못함 (worktree 구조 차이)")
    text = script.read_text()
    assert 'CHANNEL="attendance_staging"' in text
    assert 'CHANNEL="attendance_production"' in text
