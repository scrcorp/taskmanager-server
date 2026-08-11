"""HTMA(사이드로드 APK) 구버전 호환 어댑터 — **라우터/스키마 가장자리 전용**.

정책 (CLAUDE.md "HTMA API Version Policy" 3갈래 중 3번)
------------------------------------------------------
전환 창이 필요한 breaking 변경만 `X-App-Version` 요청 헤더로 분기한다.
**service/repository 안에서는 버전을 비교하지 않는다** — 버전 지식이 안쪽으로
새면 나중에 분기를 걷어낼 때 어디까지가 임시 다리인지 알 수 없게 된다.

지금 이 파일이 존재하는 이유 (force 전환 창)
--------------------------------------------
D9-1 로 스케줄 저장이 "경고만 있으면 409 → 사용자 확인 → `force:true` 재요청"
흐름으로 바뀌었다. 예전에는 **서버가 키오스크 요청에 `force=True` 를 하드코딩**해
박아넣어서 겹침 경고를 아무도 못 보고 조용히 저장됐고, 이번에 그것을 제거했다.

문제는 구버전 HTMA 다. `force` 필드를 모르니 보내지 않고 → `False` → 겹침을
만들면 **409 를 받는다.** 구버전엔 409 확인 모달이 없어서 매니저에게는 원인 모를
저장 실패로 보인다. 그래서 **구버전만 예전처럼 force 취급**한다(리스크 표 Q1:
겹침 무확인 저장은 시행 초기라 수용, min_version 하드 컷은 하지 않는다).

⚠️ 전제 — HTMA 가 `X-App-Version` 요청 헤더를 보내야 한다.
현재 HTMA 는 **응답** 헤더(`X-App-Latest-Version` 등)만 읽고 요청 헤더는 보내지
않는다. 그래서 여기서는 **헤더 없음 = 구버전**으로 처리한다. 앱이 헤더를 보내기
시작하는 릴리스(= 아래 임계 버전)부터 클라이언트 값이 존중된다.

삭제 조건 (만료일 있는 임시 다리)
---------------------------------
attendance 채널의 **min_version ≥ HTMA_FORCE_FIELD_MIN_VERSION** 이 되면
(= 구버전이 426 으로 강제 차단되면) 이 모듈과 호출부 분기를 **전부 제거**하고,
라우터는 클라이언트가 보낸 `force` 를 그대로 쓴다. 최종 상태는 분기 0개다.
"""

from __future__ import annotations

import re

from fastapi import Request


APP_VERSION_HEADER = "X-App-Version"

# force 필드를 보내기 시작하는 HTMA 릴리스. Phase 2-B(스케줄 시간 정책 앱 구현)가
# 실리는 다음 버전이다 — dev 기준 현재 1.0.16+37 이므로 그 다음이 1.0.17.
HTMA_FORCE_FIELD_MIN_VERSION: tuple[int, int, int] = (1, 0, 17)

_SEMVER_RE = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)")


def parse_app_version(raw: str | None) -> tuple[int, int, int] | None:
    """`"1.0.17+38"` / `"1.0.17"` → `(1, 0, 17)`. 못 읽으면 None.

    build number(`+38`)는 비교에 쓰지 않는다 — 릴리스 식별자는 semver 쪽이고,
    build 는 재빌드마다 올라가서 기능 유무의 근거가 되지 못한다.
    """
    if not raw:
        return None
    m = _SEMVER_RE.match(raw)
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def client_app_version(request: Request) -> tuple[int, int, int] | None:
    """요청 헤더의 클라이언트 앱 버전. 없거나 해석 불가면 None."""
    return parse_app_version(request.headers.get(APP_VERSION_HEADER))


def supports_force_field(request: Request) -> bool:
    """이 클라이언트가 `force` 를 스스로 보낼 수 있는가.

    헤더가 없으면 **구버전으로 본다** — 헤더를 보내는 것 자체가 신버전의 증거다.
    """
    version = client_app_version(request)
    if version is None:
        return False
    return version >= HTMA_FORCE_FIELD_MIN_VERSION


def effective_force(request: Request, requested: bool) -> bool:
    """구버전이면 True(예전 동작 유지), 신버전이면 클라이언트 값 존중.

    ⚠️ 이 함수는 **라우터에서만** 호출한다. service 로 넘어가는 것은 결과 bool 뿐이다.
    """
    if supports_force_field(request):
        return requested
    return True
