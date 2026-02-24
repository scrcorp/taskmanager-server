"""add_cl_instances_and_cl_completions

Revision ID: e1f2a3b4c5d6
Revises: d1e2f3a4b5c6
Create Date: 2026-02-24 12:00:00.000000

체크리스트 인스턴스(cl_instances) 및 완료 기록(cl_completions) 테이블 생성.
JSONB 스냅샷 기반에서 정규화된 테이블로 체크리스트 데이터를 분리 저장.
기존 work_assignments.checklist_snapshot 데이터를 새 테이블로 이전.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # cl_instances — 체크리스트 인스턴스 (배정 1건당 1개)
    op.create_table(
        'cl_instances',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('organization_id', UUID(as_uuid=True), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('template_id', UUID(as_uuid=True), sa.ForeignKey('checklist_templates.id', ondelete='SET NULL'), nullable=True),
        sa.Column('work_assignment_id', UUID(as_uuid=True), sa.ForeignKey('work_assignments.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('store_id', UUID(as_uuid=True), sa.ForeignKey('stores.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('work_date', sa.Date(), nullable=False),
        sa.Column('snapshot', JSONB, nullable=False),
        sa.Column('total_items', sa.Integer(), server_default='0'),
        sa.Column('completed_items', sa.Integer(), server_default='0'),
        sa.Column('status', sa.String(20), server_default='pending'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # cl_completions — 체크리스트 항목별 완료 기록
    op.create_table(
        'cl_completions',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('instance_id', UUID(as_uuid=True), sa.ForeignKey('cl_instances.id', ondelete='CASCADE'), nullable=False),
        sa.Column('item_index', sa.Integer(), nullable=False),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_timezone', sa.String(50), nullable=True),
        sa.Column('photo_url', sa.String(500), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('location', JSONB, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('instance_id', 'item_index', name='uq_cl_completion_instance_item'),
    )

    # 인덱스 — cl_instances 검색 최적화
    op.create_index('ix_cl_instances_org_id', 'cl_instances', ['organization_id'])
    op.create_index('ix_cl_instances_store_date', 'cl_instances', ['store_id', 'work_date'])
    op.create_index('ix_cl_instances_user_date', 'cl_instances', ['user_id', 'work_date'])

    # ─── 기존 JSONB 데이터 이전 ────────────────────────────────────────────────
    # work_assignments에서 checklist_snapshot이 있는 모든 row를 cl_instances로 이전
    # 완료된 항목(is_completed=true)만 cl_completions로 이전

    conn = op.get_bind()

    # 1) work_assignments → cl_instances
    assignments = conn.execute(
        sa.text("""
            SELECT id, organization_id, store_id, user_id, work_date,
                   checklist_snapshot, total_items, completed_items, status, created_at
            FROM work_assignments
            WHERE checklist_snapshot IS NOT NULL
        """)
    ).fetchall()

    for wa in assignments:
        snapshot = wa.checklist_snapshot
        if not snapshot or "items" not in snapshot:
            continue

        instance_id = uuid.uuid4()

        # template_id 추출 (snapshot에 template_id가 있으면 사용)
        template_id_str = snapshot.get("template_id")

        # cl_instance status 매핑: work_assignment의 "assigned" → cl_instance의 "pending"
        wa_status = wa.status or "assigned"
        cl_status = "pending" if wa_status == "assigned" else wa_status

        conn.execute(
            sa.text("""
                INSERT INTO cl_instances
                    (id, organization_id, template_id, work_assignment_id, store_id,
                     user_id, work_date, snapshot, total_items, completed_items, status, created_at, updated_at)
                VALUES
                    (:id, :org_id, :template_id, :wa_id, :store_id,
                     :user_id, :work_date, :snapshot, :total_items, :completed_items, :status, :created_at, :created_at)
            """),
            {
                "id": instance_id,
                "org_id": wa.organization_id,
                "template_id": uuid.UUID(template_id_str) if template_id_str else None,
                "wa_id": wa.id,
                "store_id": wa.store_id,
                "user_id": wa.user_id,
                "work_date": wa.work_date,
                "snapshot": json.dumps(snapshot),
                "total_items": wa.total_items or 0,
                "completed_items": wa.completed_items or 0,
                "status": cl_status,
                "created_at": wa.created_at or datetime.now(timezone.utc),
            },
        )

        # 2) 완료된 항목만 cl_completions로 이전
        items = snapshot.get("items", [])
        for item in items:
            if not item.get("is_completed"):
                continue

            # 기존 완료 시간: 로컬 시간 문자열 "2026-02-20T14:05" + 타임존 "PST"
            completed_at_str = item.get("completed_at")
            completed_tz = item.get("completed_tz")

            # completed_at을 UTC TIMESTAMPTZ로 변환
            # 기존 데이터는 로컬 시간 문자열이므로 정확한 UTC 변환은 어려움
            # → 로컬 시간 그대로 UTC로 저장하고, completed_timezone에 타임존 기록
            if completed_at_str:
                try:
                    completed_at = datetime.fromisoformat(completed_at_str).replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    completed_at = datetime.now(timezone.utc)
            else:
                completed_at = datetime.now(timezone.utc)

            # IANA 타임존으로 변환 (약어 → IANA 매핑)
            tz_abbrev_to_iana = {
                "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
                "MST": "America/Denver", "MDT": "America/Denver",
                "CST": "America/Chicago", "CDT": "America/Chicago",
                "EST": "America/New_York", "EDT": "America/New_York",
                "KST": "Asia/Seoul", "JST": "Asia/Tokyo",
                "UTC": "UTC", "GMT": "UTC",
            }
            iana_tz = tz_abbrev_to_iana.get(completed_tz, "America/Los_Angeles") if completed_tz else None

            conn.execute(
                sa.text("""
                    INSERT INTO cl_completions
                        (id, instance_id, item_index, user_id, completed_at, completed_timezone, created_at)
                    VALUES
                        (:id, :instance_id, :item_index, :user_id, :completed_at, :completed_timezone, :completed_at)
                """),
                {
                    "id": uuid.uuid4(),
                    "instance_id": instance_id,
                    "item_index": item.get("item_index", 0),
                    "user_id": wa.user_id,
                    "completed_at": completed_at,
                    "completed_timezone": iana_tz,
                },
            )


def downgrade() -> None:
    op.drop_index('ix_cl_instances_user_date', table_name='cl_instances')
    op.drop_index('ix_cl_instances_store_date', table_name='cl_instances')
    op.drop_index('ix_cl_instances_org_id', table_name='cl_instances')
    op.drop_table('cl_completions')
    op.drop_table('cl_instances')
