"""커스텀 HTTP 예외 클래스 모듈.

Custom HTTP exception classes module.
Provides pre-configured HTTPException subclasses for common error patterns.
These simplify error raising across services and repositories by
eliminating the need to specify status codes at each call site.

Usage:
    from app.utils.exceptions import NotFoundError, DuplicateError
    raise NotFoundError("User not found")
    raise DuplicateError("Username already exists")
"""

from fastapi import HTTPException, status


class NotFoundError(HTTPException):
    """404 Not Found 예외 — 요청한 리소스를 찾을 수 없을 때 사용.

    404 Not Found exception.
    Raised when a requested resource (user, store, assignment, etc.) does not exist.

    Args:
        detail: 오류 메시지 (Error message, default: "Resource not found")
    """

    def __init__(self, detail: str = "Resource not found") -> None:
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


class DuplicateError(HTTPException):
    """409 Conflict 예외 — 중복 리소스 생성 시도 시 사용.

    409 Conflict exception.
    Raised when attempting to create a resource that violates a uniqueness constraint
    (e.g. duplicate username, duplicate shift name within a store).

    Args:
        detail: 오류 메시지 (Error message, default: "Resource already exists")
    """

    def __init__(self, detail: str = "Resource already exists") -> None:
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)


class ForbiddenError(HTTPException):
    """403 Forbidden 예외 — 권한 부족 시 사용.

    403 Forbidden exception.
    Raised when the authenticated user lacks the required permission level
    (e.g. staff user attempting admin-only operations).

    Args:
        detail: 오류 메시지 (Error message, default: "Insufficient permissions")
    """

    def __init__(self, detail: str = "Insufficient permissions") -> None:
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


class UnauthorizedError(HTTPException):
    """401 Unauthorized 예외 — 인증 실패 시 사용.

    401 Unauthorized exception.
    Raised when authentication is missing, invalid, or expired
    (e.g. missing JWT token, expired token, invalid credentials).

    Args:
        detail: 오류 메시지 (Error message, default: "Authentication required")
    """

    def __init__(self, detail: str = "Authentication required") -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


class BadRequestError(HTTPException):
    """400 Bad Request 예외 — 잘못된 요청 데이터 시 사용.

    400 Bad Request exception.
    Raised when the request data is invalid beyond what Pydantic validation catches
    (e.g. business logic validation failures, invalid state transitions).

    Args:
        detail: 오류 메시지 (Error message, default: "Bad request")
    """

    def __init__(self, detail: str = "Bad request") -> None:
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


class ConflictError(HTTPException):
    """409 Conflict 예외 — 리소스 충돌 시 사용.

    409 Conflict exception.
    Raised when the request conflicts with existing data
    (e.g. email already registered).

    Args:
        detail: 오류 메시지 (Error message, default: "Conflict")
        detail: 추가 정보 딕셔너리 (Additional detail dict, optional)
    """

    def __init__(self, detail: str = "Conflict", **kwargs) -> None:
        error_detail = {"message": detail, **kwargs}
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=error_detail)


class AppError(HTTPException):
    """구조화 에러 — 에러 표시 UX 표준(코드 + 사용자 메시지 + 다음 행동).

    Structured error following the project error-UX standard. The ``detail`` payload is:

        {"code": "<MACHINE_CODE>", "message": "<user-facing cause>", "hint": "<next action>"}

    Why structured:
        - ``code``: 클라가 분기/로깅에 사용 (machine-readable). 발생 에러 = 표시 에러 일치 보장.
        - ``message``: 사용자에게 그대로 보여줄 "원인" 문장 (raw/기술 메시지 금지).
        - ``hint``: "다음 행동" 안내 (선택). 클라는 message 아래 보조 문구로 표시.

    클라이언트는 이 셋을 신뢰해 맥락별 위치(폼=inline / 액션=toast / 로드=배너)에 배치한다.
    임의로 일반화한 가짜 메시지를 만들지 않는다.
    """

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        hint: str | None = None,
    ) -> None:
        detail: dict[str, str] = {"code": code, "message": message}
        if hint is not None:
            detail["hint"] = hint
        super().__init__(status_code=status_code, detail=detail)


class CaptureTimeRequiredError(AppError):
    """422 — 검증맥락 사진에 촬영 시각(capture_time)이 없을 때.

    Raised only when capture-time enforcement is ON (settings.REQUIRE_CAPTURE_TIME).
    과도기에는 OFF(기본) — 시각 없는 사진도 받되 capture_source="unknown" 으로 기록한다.
    """

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="CAPTURE_TIME_REQUIRED",
            message="This photo is missing its capture time.",
            hint="Retake it with the camera, or choose a photo that still has its time information.",
        )
