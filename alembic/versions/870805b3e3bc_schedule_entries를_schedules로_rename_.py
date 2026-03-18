"""schedule_entries를_schedules로_rename_work_assignment_데이터_마이그레이션

Revision ID: 870805b3e3bc
Revises: 4a3290ce1174
Create Date: 2026-03-13 15:59:48.266501

1. 구 schedules + schedule_approvals 테이블 삭제
2. schedule_entries → schedules 테이블 rename
3. work_assignments 데이터를 schedules로 마이그레이션 (work_role 자동 생성 포함)
4. cl_instances에 schedule_id 컬럼 추가, work_assignment_id → schedule_id 매핑
5. cl_instances.work_assignment_id nullable로 변경
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '870805b3e3bc'
down_revision: Union[str, None] = '4a3290ce1174'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # ─── Step 1: 구 schedules 관련 테이블 정리 ──────────────────────

    # 1a. schedule_approvals 테이블 삭제 (FK to old schedules)
    op.drop_table("schedule_approvals")

    # 1b. 구 schedules 테이블 삭제
    # 먼저 FK constraint 제거
    op.drop_constraint("schedules_work_assignment_id_fkey", "schedules", type_="foreignkey")
    op.drop_table("schedules")

    # ─── Step 2: schedule_entries → schedules rename ─────────────────

    # 2a. FK constraints 제거 (rename 전)
    op.drop_constraint("schedule_entries_work_assignment_id_fkey", "schedule_entries", type_="foreignkey")

    # 2b. work_assignment_id 컬럼 삭제 (더 이상 불필요)
    op.drop_column("schedule_entries", "work_assignment_id")

    # 2c. 인덱스 삭제 (rename 후 재생성)
    op.drop_index("ix_schedule_entries_org_store_date", table_name="schedule_entries")
    op.drop_index("ix_schedule_entries_user_date", table_name="schedule_entries")
    op.drop_index("ix_schedule_entries_period", table_name="schedule_entries")

    # 2d. 테이블 rename
    op.rename_table("schedule_entries", "schedules")

    # 2e. 인덱스 재생성 (새 테이블명으로)
    op.create_index("ix_schedules_org_store_date", "schedules", ["organization_id", "store_id", "work_date"])
    op.create_index("ix_schedules_user_date", "schedules", ["user_id", "work_date"])
    op.create_index("ix_schedules_period", "schedules", ["period_id"])

    # ─── Step 3: cl_instances에 schedule_id 컬럼 추가 ────────────────

    op.add_column("cl_instances", sa.Column("schedule_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint("uq_cl_instances_schedule_id", "cl_instances", ["schedule_id"])
    op.create_foreign_key(
        "cl_instances_schedule_id_fkey",
        "cl_instances", "schedules",
        ["schedule_id"], ["id"],
        ondelete="SET NULL",
    )

    # ─── Step 4: work_assignments → schedules 데이터 마이그레이션 ────

    # 4a. work_role 자동 생성 — checklist_template이 존재하는 store+shift+position 조합만 생성
    # (체크리스트 없는 조합은 work_role 생성 안 함, default_checklist_id도 함께 설정)
    conn.execute(sa.text("""
        INSERT INTO store_work_roles (id, store_id, shift_id, position_id, name, default_checklist_id, is_active, sort_order, created_at, updated_at)
        SELECT
            gen_random_uuid(),
            wa.store_id,
            wa.shift_id,
            wa.position_id,
            COALESCE(s.name, '') || ' - ' || COALESCE(p.name, ''),
            ct.id,
            true,
            0,
            now(),
            now()
        FROM (
            SELECT DISTINCT store_id, shift_id, position_id
            FROM work_assignments
        ) wa
        JOIN checklist_templates ct
          ON ct.store_id = wa.store_id
         AND ct.shift_id = wa.shift_id
         AND ct.position_id = wa.position_id
        LEFT JOIN shifts s ON s.id = wa.shift_id
        LEFT JOIN positions p ON p.id = wa.position_id
        WHERE NOT EXISTS (
            SELECT 1 FROM store_work_roles swr
            WHERE swr.store_id = wa.store_id
              AND swr.shift_id = wa.shift_id
              AND swr.position_id = wa.position_id
        )
    """))

    # 4b. start_time을 nullable로 임시 변경 (마이그레이션 데이터에 시간 없음)
    op.alter_column("schedules", "start_time", nullable=True)
    op.alter_column("schedules", "end_time", nullable=True)

    # 4c. work_assignments 데이터를 schedules에 삽입
    conn.execute(sa.text("""
        INSERT INTO schedules (
            id, organization_id, period_id, request_id,
            user_id, store_id, work_role_id, work_date,
            start_time, end_time, break_start_time, break_end_time,
            net_work_minutes, status, created_by, approved_by,
            note, created_at, updated_at
        )
        SELECT
            wa.id,
            wa.organization_id,
            NULL,                          -- period_id (없음)
            NULL,                          -- request_id (없음)
            wa.user_id,
            wa.store_id,
            swr.id,                        -- work_role_id (매칭된 work_role)
            wa.work_date,
            COALESCE(swr.default_start_time, NULL),  -- start_time (work_role 기본값 또는 NULL)
            COALESCE(swr.default_end_time, NULL),    -- end_time
            swr.break_start_time,          -- break_start_time
            swr.break_end_time,            -- break_end_time
            CASE
                WHEN swr.default_start_time IS NOT NULL AND swr.default_end_time IS NOT NULL
                THEN EXTRACT(EPOCH FROM (swr.default_end_time - swr.default_start_time)) / 60
                     - COALESCE(
                         EXTRACT(EPOCH FROM (swr.break_end_time - swr.break_start_time)) / 60,
                         0
                       )
                ELSE 0
            END::integer,                  -- net_work_minutes
            CASE wa.status
                WHEN 'completed' THEN 'confirmed'
                WHEN 'in_progress' THEN 'confirmed'
                WHEN 'pending' THEN 'confirmed'
                ELSE 'confirmed'
            END,                           -- status
            wa.assigned_by,                -- created_by
            wa.assigned_by,                -- approved_by
            NULL,                          -- note
            wa.created_at,
            wa.updated_at
        FROM work_assignments wa
        JOIN store_work_roles swr
          ON swr.store_id = wa.store_id
         AND swr.shift_id = wa.shift_id
         AND swr.position_id = wa.position_id
    """))

    # ─── Step 5: cl_instances.work_assignment_id → schedule_id 매핑 ──

    # work_assignment의 id를 그대로 schedule id로 사용했으므로 직접 매핑 가능
    conn.execute(sa.text("""
        UPDATE cl_instances
        SET schedule_id = work_assignment_id
        WHERE work_assignment_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM schedules WHERE id = cl_instances.work_assignment_id)
    """))

    # ─── Step 6: cl_instances.work_assignment_id nullable로 변경 ─────

    op.drop_constraint("cl_instances_work_assignment_id_fkey", "cl_instances", type_="foreignkey")
    op.alter_column("cl_instances", "work_assignment_id", nullable=True)
    # FK 재생성 (nullable)
    op.create_foreign_key(
        "cl_instances_work_assignment_id_fkey",
        "cl_instances", "work_assignments",
        ["work_assignment_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    conn = op.get_bind()

    # ─── Reverse Step 6: cl_instances.work_assignment_id NOT NULL ────
    op.drop_constraint("cl_instances_work_assignment_id_fkey", "cl_instances", type_="foreignkey")
    # Clear schedule_id mapping
    conn.execute(sa.text("UPDATE cl_instances SET schedule_id = NULL"))
    # Restore NOT NULL (only if all rows have work_assignment_id)
    op.alter_column("cl_instances", "work_assignment_id", nullable=False)
    op.create_foreign_key(
        "cl_instances_work_assignment_id_fkey",
        "cl_instances", "work_assignments",
        ["work_assignment_id"], ["id"],
        ondelete="SET NULL",
    )

    # ─── Reverse Step 5: Remove migrated schedule rows (from work_assignments) ──
    conn.execute(sa.text("""
        DELETE FROM schedules
        WHERE id IN (SELECT id FROM work_assignments)
    """))

    # ─── Reverse Step 4b: Restore NOT NULL on start_time/end_time ──
    op.alter_column("schedules", "start_time", nullable=False)
    op.alter_column("schedules", "end_time", nullable=False)

    # ─── Reverse Step 3: Remove schedule_id from cl_instances ────────
    op.drop_constraint("cl_instances_schedule_id_fkey", "cl_instances", type_="foreignkey")
    op.drop_constraint("uq_cl_instances_schedule_id", "cl_instances", type_="unique")
    op.drop_column("cl_instances", "schedule_id")

    # ─── Reverse Step 2: schedules → schedule_entries ────────────────
    op.drop_index("ix_schedules_org_store_date", table_name="schedules")
    op.drop_index("ix_schedules_user_date", table_name="schedules")
    op.drop_index("ix_schedules_period", table_name="schedules")

    op.rename_table("schedules", "schedule_entries")

    op.create_index("ix_schedule_entries_org_store_date", "schedule_entries", ["organization_id", "store_id", "work_date"])
    op.create_index("ix_schedule_entries_user_date", "schedule_entries", ["user_id", "work_date"])
    op.create_index("ix_schedule_entries_period", "schedule_entries", ["period_id"])

    # Restore work_assignment_id column
    op.add_column("schedule_entries", sa.Column("work_assignment_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "schedule_entries_work_assignment_id_fkey",
        "schedule_entries", "work_assignments",
        ["work_assignment_id"], ["id"],
        ondelete="SET NULL",
    )

    # ─── Reverse Step 1: Recreate old schedules + schedule_approvals ─
    op.create_table(
        "schedules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("store_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("shift_id", sa.Uuid(), nullable=True),
        sa.Column("position_id", sa.Uuid(), nullable=True),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("work_assignment_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["work_assignment_id"], ["work_assignments.id"], name="schedules_work_assignment_id_fkey"),
    )

    op.create_table(
        "schedule_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("schedule_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["schedule_id"], ["schedules.id"], name="schedule_approvals_schedule_id_fkey"),
    )
