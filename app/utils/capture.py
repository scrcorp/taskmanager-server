"""촬영 시각(capture_time) 정규화 + 강제 검증 — 검증맥락 사진 신뢰 보조.

검증맥락 사진(체크리스트 완료 증거 등)은 "언제 찍혔는지"가 중요하다. 신뢰 앵커는
**서버 수신시각**(파일 row의 created_at)이며, capture_time 은 클라이언트가 주장하는
촬영시각이다(라이브=셔터시각, 갤러리=EXIF). 콘솔이 둘의 델타와 capture_source 로
사기 신호를 표시한다.

과도기 정책(받되 플래그): capture_time 이 없어도 저장은 허용하되 capture_source="unknown"
으로 기록한다. settings.REQUIRE_CAPTURE_TIME 이 True 로 전환되면(앱 배포 완료 후)
체크리스트 완료 경로에서 capture_time 없는 사진을 422 로 거부한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.utils.exceptions import CaptureTimeRequiredError

# 클라이언트가 보낼 수 있는 출처 값 (그 외/누락은 "unknown" 으로 정규화)
VALID_SOURCES: frozenset[str] = frozenset({"live", "gallery", "unknown"})


@dataclass
class NormalizedPhoto:
    """정규화된 사진 1장 — 항상 capture_source 가 채워져 있다."""

    key: str
    capture_time: datetime | None
    capture_source: str  # live | gallery | unknown (누락 시 "unknown")


def normalize_photos(
    photos: list | None,
    photo_urls: list[str] | None,
    photo_url: str | None,
) -> list[NormalizedPhoto]:
    """photos(신규, 메타 포함) > photo_urls > photo_url 우선순위로 정규화한다.

    legacy 경로(문자열 키만)는 capture_time=None, capture_source="unknown".
    photos 경로에서 capture_source 가 누락/비정상이면 "unknown" 으로 보수적 정규화한다
    (capture_time 만 있다고 "live" 로 추정하지 않는다 — 출처 미상이면 미상).
    """
    if photos:
        result: list[NormalizedPhoto] = []
        for p in photos:
            src = p.capture_source if p.capture_source in VALID_SOURCES else "unknown"
            result.append(NormalizedPhoto(key=p.key, capture_time=p.capture_time, capture_source=src))
        return result
    if photo_urls:
        return [NormalizedPhoto(key=k, capture_time=None, capture_source="unknown") for k in photo_urls]
    if photo_url:
        return [NormalizedPhoto(key=photo_url, capture_time=None, capture_source="unknown")]
    return []


def enforce_capture_time(normalized: list[NormalizedPhoto], *, required: bool) -> None:
    """required=True 이고 capture_time 없는 사진이 하나라도 있으면 거부한다.

    required=False(과도기 기본)면 아무 것도 하지 않는다(받되 플래그).
    """
    if not required:
        return
    if any(p.capture_time is None for p in normalized):
        raise CaptureTimeRequiredError()
