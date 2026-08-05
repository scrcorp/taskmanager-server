"""다운로드 파일명 + Content-Disposition 헤더 — 유니코드(한글) 보존.

파일명에서 한글이 사라지면 받는 사람이 어느 매장/직원 것인지 못 알아본다.
그래서 이름은 유니코드 그대로 두고, 전송 계층에서만 규격을 맞춘다:

    Content-Disposition: attachment; filename="ASCII fallback";
                         filename*=UTF-8''%ED%95%9C%EA%B8%80...

RFC 6266/5987 — 현대 브라우저는 filename* 를 쓰고, 못 읽는 구형 클라이언트만
ASCII fallback 으로 떨어진다. 헤더는 latin-1 로 인코딩되므로 filename= 쪽에
비 ASCII 를 넣으면 서버가 500 을 낸다 (fallback 이 필요한 진짜 이유).
"""

from __future__ import annotations

import re
from urllib.parse import quote

# 파일시스템/헤더에서 문제가 되는 문자만 — 한글·기호는 건드리지 않는다
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WHITESPACE = re.compile(r"\s+")
_REPEATED_UNDERSCORE = re.compile(r"_{2,}")

_FALLBACK_NAME = "download"


def safe_filename(name: str) -> str:
    """이름 조각 1개를 파일명에 안전하게 — 불가 문자만 '_' 로 접는다.

    유니코드는 보존한다 (한글 매장명/직원명이 파일명에 그대로 남는 게 목적).
    공백은 제거 — dailyreport/stub 파일명 관례와 같고, 따옴표 없이 다루는
    클라이언트에서 이름이 잘리는 사고를 막는다.
    """
    cleaned = _ILLEGAL.sub("_", name)
    cleaned = _WHITESPACE.sub("", cleaned)
    cleaned = _REPEATED_UNDERSCORE.sub("_", cleaned).strip("._-")
    return cleaned or _FALLBACK_NAME


def _ascii_only(text: str) -> str:
    stripped = "".join(
        ch for ch in text if ch.isascii() and ch.isprintable() and ch != '"'
    )
    return _REPEATED_UNDERSCORE.sub("_", stripped).strip("._-")


def ascii_fallback(filename: str) -> str:
    """비 ASCII 를 걷어낸 파일명 — Content-Disposition 의 filename= 파라미터용.

    확장자는 따로 떼어 보존한다 — 이름이 통째로 비 ASCII 여도(예: "한글.pdf")
    클라이언트가 확장자로 뭘 여는지 알 수 있어야 한다.
    """
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    ascii_stem = _ascii_only(stem) or _FALLBACK_NAME
    ascii_ext = _ascii_only(ext)
    return f"{ascii_stem}.{ascii_ext}" if ascii_ext else ascii_stem


def content_disposition(filename: str, *, disposition: str = "attachment") -> str:
    """유니코드 파일명 → Content-Disposition 헤더 값 (fallback 포함)."""
    encoded = quote(filename, safe="")
    return (
        f'{disposition}; filename="{ascii_fallback(filename)}"; '
        f"filename*=UTF-8''{encoded}"
    )
