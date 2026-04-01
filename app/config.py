"""애플리케이션 환경 설정 모듈.

Application configuration module using pydantic-settings.
All settings can be overridden via environment variables or a .env file.
"""

from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings

# .env 파일 절대 경로 — CWD와 무관하게 항상 server/.env를 참조
# Absolute path to .env file — ensures correct loading regardless of CWD
_ENV_FILE: Path = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """애플리케이션 전역 설정 — 환경 변수 기반 구성.

    Global application settings loaded from environment variables.
    Uses pydantic-settings for automatic env var parsing and .env file support.

    Attributes:
        DATABASE_URL: PostgreSQL 비동기 연결 문자열 (Async PostgreSQL connection string)
        JWT_SECRET_KEY: JWT 서명 비밀키 (JWT signing secret key)
        JWT_ALGORITHM: JWT 서명 알고리즘 (JWT signing algorithm)
        JWT_ACCESS_TOKEN_EXPIRE_MINUTES: 액세스 토큰 만료 시간(분) (Access token TTL in minutes)
        JWT_REFRESH_TOKEN_EXPIRE_DAYS: 리프레시 토큰 만료 시간(일) (Refresh token TTL in days)
        CORS_ORIGINS: 허용된 CORS 출처 목록 (Allowed CORS origin URLs)
        APP_NAME: 애플리케이션 표시 이름 (Application display name)
        DEBUG: 디버그 모드 플래그 (Debug mode flag, enables SQL echo)
    """

    # 데이터베이스 — PostgreSQL async 연결 URL (asyncpg 드라이버 사용)
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/employee_mgmt"

    # JWT 인증 설정 — JSON Web Token authentication settings
    JWT_SECRET_KEY: str = "change-this-secret-key-in-production"  # 운영 환경에서 반드시 변경 (MUST change in production)
    JWT_ALGORITHM: str = "HS256"  # HMAC-SHA256 대칭 서명 (Symmetric signing algorithm)
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30  # 액세스 토큰 유효 기간: 30분 (Access token TTL)
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7  # 리프레시 토큰 유효 기간: 7일 (Refresh token TTL)

    # CORS 설정 — 프론트엔드 개발 서버 허용 (Frontend dev server origins)
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:8080"]

    # 앱 메타데이터 — Application metadata
    APP_NAME: str = "TaskManager API"
    DEBUG: bool = True  # True이면 SQLAlchemy SQL 로그 출력 (Enables SQL echo when True)

    # Axiom 로깅 설정 — Axiom observability platform settings
    AXIOM_API_TOKEN: str = ""  # Axiom API 토큰 (API token from Axiom dashboard)
    AXIOM_DATASET: str = ""  # Axiom 데이터셋 이름 (Dataset name for API logs)

    # Storage 모드 — "local" 또는 "s3" (s3일 때 access key 없으면 IAM role 사용)
    STORAGE_MODE: str = "local"

    # AWS S3 설정 — 파일 업로드 presigned URL 생성용
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_S3_BUCKET: str = ""
    AWS_S3_REGION: str = "ap-northeast-2"
    # S3 fallback 버킷 — staging용: 현재 버킷에 없으면 여기서 복사 (비어있으면 fallback 없음)
    STORAGE_FALLBACK_BUCKET: str = ""

    # 로컬 버킷 디렉토리 — local 모드에서 필수 (비어있으면 에러)
    LOCAL_BUCKET_DIR: str = ""
    # 로컬 fallback 버킷 — worktree용: 현재 버킷에 없으면 여기서 복사 (비어있으면 fallback 없음)
    LOCAL_FALLBACK_BUCKET_DIR: str = ""

    # 폴더별 저장 경로 (S3 key prefix / 로컬 하위 폴더명)
    STORAGE_FOLDER_REVIEWS: str = "reviews"
    STORAGE_FOLDER_COMPLETIONS: str = "completions"
    STORAGE_FOLDER_PROFILES: str = "profiles"
    STORAGE_FOLDER_ANNOUNCEMENTS: str = "announcements"
    STORAGE_FOLDER_ISSUES: str = "issues"

    # 체크리스트 사진 최대 장수 — 플랫폼 전체 상수
    MAX_PHOTOS_PER_ITEM: int = 5

    # 체크리스트 샘플 Excel 경로 — 비어있으면 server/static/checklist_template_sample.xlsx 사용
    CHECKLIST_SAMPLE_EXCEL_PATH: str = ""
    # 인벤토리 Import 템플릿 Excel 파일명 — static/ 폴더 내 파일명
    INVENTORY_TEMPLATE_EXCEL: str = "inventory_import_template.xlsx"

    # SMTP 이메일 설정 — Brevo (smtp-relay.brevo.com)
    SMTP_HOST: str = "smtp-relay.brevo.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""  # Brevo 계정 이메일
    SMTP_PASSWORD: str = ""  # Brevo SMTP 키 (API 키 아님)
    SMTP_FROM_EMAIL: str = ""  # 발신 이메일 주소
    SMTP_FROM_NAME: str = "TaskManager"  # 발신자 표시 이름

    # 보고서 제출 알림 수신 이메일 — Daily Report submit 시 알림 발송
    REPORT_NOTIFICATION_EMAIL: str = ""

    # Server 베이스 URL — 로컬 이미지 URL 등에서 사용 (비어있으면 http://localhost:{port})
    SERVER_BASE_URL: str = ""

    # Admin 콘솔 베이스 URL — 이메일 링크 등에서 사용
    ADMIN_BASE_URL: str = "https://console.hermesops.site"

    model_config = {"env_file": _ENV_FILE, "env_file_encoding": "utf-8"}


# 전역 설정 싱글턴 인스턴스 — Global settings singleton instance
settings: Settings = Settings()
