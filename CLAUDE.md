# Employee Management Service — Backend

> **IMPORTANT**: Before implementing any feature, read the parent `../CLAUDE.md` and relevant task docs in `../docs/02_plan/`.
> Task documents are the Source of Truth for API paths, table names, permissions, and status values.

## Project Overview

Multi-store employee management system. FastAPI + PostgreSQL (AWS RDS) backend serving two frontends: Admin (Next.js on Vercel) and App (Flutter Web on S3+CloudFront). EC2에서 HTTP로 운영, Vercel/CloudFront가 HTTPS proxy.

## Tech Stack

- **Runtime**: Python 3.12+
- **Framework**: FastAPI (async)
- **ORM**: SQLAlchemy 2.0 (async) + asyncpg
- **Validation**: Pydantic v2
- **Auth**: JWT (PyJWT) + bcrypt (passlib)
- **Database**: PostgreSQL (AWS RDS, 로컬은 개별 PostgreSQL)
- **Migration**: Alembic

## Project Structure

```
server/
├── CLAUDE.md              ← You are here
├── requirements.txt
├── alembic.ini
├── alembic/
│   └── versions/
├── app/
│   ├── main.py            ← FastAPI app factory
│   ├── config.py           ← Settings (env vars)
│   ├── database.py         ← Async engine + session
│   ├── models/             ← SQLAlchemy models
│   │   ├── __init__.py
│   │   ├── organization.py  (organizations, stores)
│   │   ├── user.py          (roles, users)
│   │   ├── user_store.py    (user_stores)
│   │   ├── work.py          (shifts, positions)
│   │   ├── checklist.py     (checklist_templates, checklist_template_items)
│   │   ├── assignment.py    (work_assignments)
│   │   ├── communication.py (announcements, additional_tasks, additional_task_assignees)
│   │   ├── notification.py  (notifications)
│   │   └── media.py         (media — Phase 4)
│   ├── schemas/            ← Pydantic request/response
│   │   ├── __init__.py
│   │   ├── auth.py          (Login, Register, Token, UserMe)
│   │   ├── organization.py  (Organization, Store schemas)
│   │   ├── user.py           (Role, User, Profile schemas)
│   │   ├── work.py           (Shift, Position schemas)
│   │   └── common.py         (Checklist, Assignment, Announcement, Task, Notification)
│   ├── services/           ← Business logic
│   │   ├── __init__.py
│   │   ├── auth_service.py
│   │   ├── organization_service.py
│   │   ├── store_service.py
│   │   ├── user_service.py
│   │   ├── shift_service.py
│   │   ├── position_service.py
│   │   ├── checklist_service.py
│   │   ├── assignment_service.py
│   │   ├── announcement_service.py
│   │   ├── task_service.py
│   │   ├── notification_service.py
│   │   └── profile_service.py
│   ├── repositories/       ← DB queries only
│   │   └── (mirrors services/)
│   ├── api/
│   │   ├── __init__.py
│   │   ├── deps.py          ← Dependency injection (get_db, get_current_user)
│   │   ├── admin/           ← /api/v1/admin/*
│   │   │   ├── __init__.py
│   │   │   ├── auth.py
│   │   │   ├── organizations.py
│   │   │   ├── stores.py
│   │   │   ├── shifts.py
│   │   │   ├── positions.py
│   │   │   ├── roles.py
│   │   │   ├── users.py
│   │   │   ├── checklists.py
│   │   │   ├── assignments.py
│   │   │   ├── announcements.py
│   │   │   ├── tasks.py
│   │   │   └── notifications.py
│   │   └── app/             ← /api/v1/app/*
│   │       ├── __init__.py
│   │       ├── auth.py
│   │       ├── assignments.py
│   │       ├── tasks.py
│   │       ├── announcements.py
│   │       └── notifications.py
│   ├── middleware/
│   │   ├── __init__.py
│   │   └── axiom_logging.py  ← Axiom 로그 미들웨어
│   └── utils/
│       ├── __init__.py
│       ├── jwt.py
│       ├── password.py
│       ├── pagination.py
│       └── exceptions.py
└── tests/
```

## Architecture Pattern

3-Layer: **Router → Service → Repository**

- **Router**: HTTP handling, Pydantic validation, call service, return response
- **Service**: Business logic, transaction management. Example: assignment creation = create assignment + generate snapshot + send notification
- **Repository**: Pure DB queries via SQLAlchemy. No business logic.

## Development Phases

Build in this order. Each phase should be fully working before moving to next.

### Phase 1 — Foundation (27 endpoints)

1. Project setup: FastAPI app, config, database connection
2. Auth: JWT encode/decode, bcrypt, login/register endpoints
3. Organization CRUD (admin only)
4. Store CRUD (admin: full, scoped to org)
5. Role CRUD (admin only, level-based hierarchy)
6. User CRUD (admin: manage all users, app: self-register)
7. Shift CRUD (under stores)
8. Position CRUD (under stores)

### Phase 2 — Core Workflow (18 endpoints)

9. Checklist Template CRUD (store x shift x position unique)
10. Template Item CRUD (sort_order, drag reorder)
11. Work Assignment creation + JSONB snapshot generation
12. Assignment list/filter (by date, store, user, status)
13. Checklist completion (JSONB item update, auto status change)

### Phase 3 — Communication (25 endpoints)

14. Announcement CRUD (org-wide or store-specific)
15. Additional Task CRUD + assignee management
16. Notification auto-creation (on assignment, task, announcement)
17. Notification read/mark-all-read

## Key Implementation Details

### JWT Payload
```python
{
    "sub": "user_uuid",
    "org": "organization_uuid",
    "role": "supervisor",
    "level": 3,
    "exp": timestamp
}
```

### Auth Separation
- `POST /api/v1/admin/auth/login` → Reject role level >= 4 (staff)
- `POST /api/v1/app/auth/login` → Allow staff (level 4) + supervisor (level 3)

### JSONB Snapshot (Work Assignment)
When creating a work_assignment, snapshot the current checklist template:
```python
checklist_snapshot = [
    {
        "item_index": 0,
        "title": "Preheat grill",
        "description": "400°F",
        "verification_type": "none",
        "is_completed": False,
        "completed_at": None
    },
    ...
]
```

### Notification Types
- `work_assigned` → reference_type: "work_assignment"
- `additional_task` → reference_type: "additional_task"
- `announcement` → reference_type: "announcement"

### Organization Scoping
Every query MUST filter by organization_id from JWT. Never return cross-org data.

## Environment Variables

```env
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname
JWT_SECRET_KEY=your-secret-key
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=30
JWT_REFRESH_TOKEN_EXPIRE_DAYS=7
CORS_ORIGINS=["http://localhost:3000","http://localhost:8080"]
```

## Commands

```bash
# Install
pip install -r requirements.txt

# Run dev
uvicorn app.main:app --reload --port 8000

# Alembic migration
alembic revision --autogenerate -m "description"  # 반드시 autogenerate 사용, revision ID 직접 지정 금지
alembic upgrade head                              # migration 생성 후 반드시 실행하여 검증

# Tests
pytest tests/ -v
```

## Alembic Migration 규칙

1. **`alembic revision --autogenerate -m "설명"`으로 생성** — revision ID를 직접 지정하지 않는다 (자동 해시 사용)
2. 생성된 파일의 `upgrade()`/`downgrade()` 검토 후 누락/오류만 수동 수정
3. autogenerate가 감지 못하는 케이스 (수동 수정 필요):
   - 테이블/컬럼 이름 변경 (drop+create로 인식됨)
   - 데이터 migration (INSERT, UPDATE 등)
   - Enum 타입 변경
   - 인덱스명만 변경
4. **`alembic upgrade head` 실행하여 검증** 후 커밋
5. 기존 데이터와 충돌 가능한 migration은 alembic 코드 내에서 해결 (배포서버에서도 동일하게 동작해야 함)

## Git Workflow

> 상세 규칙은 `../CLAUDE.md`의 "Git Branch Workflow" 참조.

- 브랜치 prefix: `feat/*`, `fix/*`, `docs/*`, `refactor/*`, `chore/*` (업무 성격에 맞게)
- **dev 머지는 반드시 사용자 허락 후 진행**
- docs 같은 경량 작업은 main에서 직접 분기 허용
- **AI Agent는 작업 시 무조건 worktree 사용**

## Coding Conventions

- Use `async/await` everywhere
- Type hints on all function signatures
- Pydantic models for all request/response
- HTTPException for error responses with proper status codes
- Use dependency injection for db session and current user
- All IDs are UUID
- All timestamps are UTC with timezone (TIMESTAMPTZ)
- snake_case for Python, camelCase for JSON response (via Pydantic alias)
