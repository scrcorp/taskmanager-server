"""초기 데이터 시드 스크립트 — 조직, 역할, 관리자 계정 생성.

Seed script — Creates initial organization, roles, and admin user.
Run this script once to bootstrap the database with required initial data.

Usage:
    python -m app.seed

Creates:
    - 1개 조직: "Withers Corporation" (1 organization)
    - 4개 역할: owner(1), general_manager(2), supervisor(3), staff(4) (4 roles)
    - 1개 관리자 계정: admin / admin123 (1 owner user)
"""

import asyncio
from app.database import async_session, engine, Base
from app.models import Organization, Role, User
from app.utils.password import hash_password


async def seed() -> None:
    """데이터베이스를 초기 데이터로 시드합니다.

    Seed the database with initial data.
    Creates tables if they don't exist, then inserts the initial
    organization, role hierarchy, and admin user.

    Idempotent: 이미 시드된 경우 건너뜁니다 (Skips if already seeded).
    """
    # 테이블 생성 — DDL 실행 (Create all tables from ORM metadata)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as db:
        # 이미 시드되었는지 확인 — 조직이 하나라도 있으면 건너뜀
        # (Check if already seeded by looking for any existing organization)
        from sqlalchemy import select
        result = await db.execute(select(Organization).limit(1))
        if result.scalar_one_or_none():
            print("Already seeded. Skipping.")
            return

        # 기본 조직 생성 — Default organization (tenant)
        org: Organization = Organization(name="Withers Corporation")
        db.add(org)
        await db.flush()  # flush로 org.id 생성 (Flush to generate org.id)

        # 역할 계층 생성 — Create role hierarchy (level 1=owner ~ 4=staff)
        roles_data: list[tuple[str, int]] = [
            ("owner", 1),
            ("general_manager", 2),
            ("supervisor", 3),
            ("staff", 4),
        ]
        roles: dict[str, Role] = {}
        for name, level in roles_data:
            role: Role = Role(organization_id=org.id, name=name, level=level)
            db.add(role)
            await db.flush()  # flush로 role.id 생성 (Flush to generate role.id)
            roles[name] = role

        # 관리자 계정 생성 — Create initial owner user (admin/admin123)
        admin: User = User(
            organization_id=org.id,
            role_id=roles["owner"].id,
            username="admin",
            full_name="System Admin",
            email="admin@withers.com",
            password_hash=hash_password("admin123"),
            is_active=True,
        )
        db.add(admin)

        await db.commit()
        print(f"Seeded: org={org.id}, admin user=admin/admin123")


if __name__ == "__main__":
    asyncio.run(seed())
