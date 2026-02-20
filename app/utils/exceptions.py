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
