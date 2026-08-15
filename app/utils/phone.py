"""전화번호 정규화 — 검색용 숫자 전용 표현 단일 구현.

계약 (docs/99_inbox/2026-08-14-연락처-API계약.md §4.1):
    - 숫자(0-9)만 남긴다. '+', '-', '(', ')', 공백, '.', 'ext' 등은 전부 제거.
    - 결과가 빈 문자열이면 None (저장은 되지만 검색에 안 걸림).
    - 선행 국가코드 '1' 은 제거하지 않는다. 검색이 부분일치라
      '2135550142' 로도 '12135550142' 가 걸린다.

서버 단일 구현 — 클라이언트는 정규화하지 않고 원문을 그대로 보낸다.
"""

__all__ = ["normalize_phone"]


def normalize_phone(value: str | None) -> str | None:
    """전화번호(또는 검색어)에서 숫자만 뽑아 반환. 숫자가 없으면 None.

    Args:
        value: 사용자가 입력한 원본 표기 또는 검색어.

    Returns:
        숫자만 남긴 문자열. 숫자가 하나도 없거나 입력이 None 이면 None.
    """
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isascii() and ch.isdigit())
    return digits or None
