# Employee Management Service — Backend

## Project Overview

Multi-store employee management system. FastAPI + PostgreSQL (Supabase) backend serving two frontends: Admin (Next.js) and App (Flutter Web).

## Tech Stack

- **Runtime**: Python 3.12+
- **Framework**: FastAPI (async)
- **ORM**: SQLAlchemy 2.0 (async) + asyncpg
- **Validation**: Pydantic v2
- **Auth**: JWT (PyJWT) + bcrypt (passlib)
- **Database**: PostgreSQL (Supabase)
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
alembic revision --autogenerate -m "description"
alembic upgrade head

# Tests
pytest tests/ -v
```

## Coding Conventions

- Use `async/await` everywhere
- Type hints on all function signatures
- Pydantic models for all request/response
- HTTPException for error responses with proper status codes
- Use dependency injection for db session and current user
- All IDs are UUID
- All timestamps are UTC with timezone (TIMESTAMPTZ)
- snake_case for Python, camelCase for JSON response (via Pydantic alias)
