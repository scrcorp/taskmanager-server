"""이메일 주소 정규화 — `users.email` canonical 형태의 단일 규칙.

`users.email` 은 trim + 소문자 형태로만 저장한다.

조회·중복체크 경로는 전부 입력을 `.strip().lower()` 한 뒤 컬럼과 `==` 비교하는데,
저장값이 사용자 입력 원본이면 대문자가 섞인 순간 영원히 매칭되지 않는다.
그 결과 실제로 (1) 같은 이메일로 계정이 중복 생성됐고 (2) 해당 계정들은
비밀번호 재설정/아이디 찾기가 동작하지 않았다.

따라서 `users.email` 에 값을 쓰는 경로는 반드시 이 헬퍼를 거친다.
스키마로 들어오는 입력은 field_validator 로, 스키마를 안 거치는 내부 경로
(유령 계정 인수, 채용 확정, 백오피스 org 생성)는 직접 호출해서 정규화한다.

메일 발송은 app/utils/email.py — 이 모듈은 주소 문자열만 다룬다.
"""


def normalize_email(v: str) -> str:
    """trim + 소문자. 필수 이메일 필드용."""
    return (v or "").strip().lower()


def normalize_email_optional(v: str | None) -> str | None:
    """trim + 소문자, 빈 값은 None. 선택 이메일 필드용."""
    if v is None:
        return None
    return normalize_email(v) or None
