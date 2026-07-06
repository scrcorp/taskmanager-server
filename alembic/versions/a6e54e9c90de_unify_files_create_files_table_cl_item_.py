"""unify files: files registry + file_usages, drop cl_item_files

Revision ID: a6e54e9c90de
Revises: a4673e5f2cb1
Create Date: 2026-06-23 13:59:20.371169

체크리스트 파일 저장을 둘로 분리한다:
- files: 순수 레지스트리. 1 물리파일 = 1 행 (path UNIQUE). "파일이 무엇인지"만.
- file_usages: 중앙 usage(junction). "이 파일이 어디서 쓰이나" (owner_type/owner_id).
  한 files 행을 여러 usage 가 가리킴 = 재사용(복사 없음).

기존 cl_item_files(5512행) → backfill:
- files: DISTINCT path 당 1행 (중복 file_url = 재제출 공유 사진은 1행으로 합쳐짐).
- file_usages: cl_item_files 1행당 1행 (owner_type='cl_item', owner_id=item_id, path 로 files 매칭).
그 뒤 cl_item_files 폐기.

blob 삭제는 이 마이그레이션 범위 아님 — 런타임은 usage 행만 삭제하고, 별도 GC 가
usage 없는 files 를 회수한다(file_service.gc_orphan_files).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a6e54e9c90de'
down_revision: Union[str, None] = 'a4673e5f2cb1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── files (순수 레지스트리, path UNIQUE) ──────────────────
    op.create_table(
        'files',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=True),
        sa.Column('store_id', sa.Uuid(), nullable=True),
        sa.Column('path', sa.String(length=500), nullable=False),
        sa.Column('file_type', sa.String(length=20), nullable=False),
        sa.Column('mime_type', sa.String(length=100), nullable=True),
        sa.Column('original_filename', sa.String(length=255), nullable=True),
        sa.Column('size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('uploaded_by', sa.Uuid(), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_files_path', 'files', ['path'], unique=True)
    op.create_index('ix_files_org_store', 'files', ['organization_id', 'store_id'], unique=False)
    op.create_index('ix_files_status', 'files', ['status'], unique=False)
    op.create_index('ix_files_uploaded_by', 'files', ['uploaded_by'], unique=False)

    # ── file_usages (중앙 usage) ──────────────────────────────
    op.create_table(
        'file_usages',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('file_id', sa.Uuid(), nullable=False),
        sa.Column('owner_type', sa.String(length=40), nullable=False),
        sa.Column('owner_id', sa.Uuid(), nullable=False),
        sa.Column('context', sa.String(length=20), nullable=True),
        sa.Column('context_id', sa.Uuid(), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['file_id'], ['files.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_file_usages_owner', 'file_usages', ['owner_type', 'owner_id'], unique=False)
    op.create_index('ix_file_usages_file_id', 'file_usages', ['file_id'], unique=False)
    op.create_index('ix_file_usages_context', 'file_usages', ['context', 'context_id'], unique=False)

    # ── backfill files: DISTINCT path 당 1행 ──────────────────
    # 같은 file_url 이 여러 cl_item_files 에 있으면(재제출 공유) 1 files 행으로 합침.
    # org/store/uploaded_by/file_type/created_at 는 그 path 의 가장 이른 행 기준.
    op.execute(
        """
        INSERT INTO files (id, organization_id, store_id, path, file_type, status, uploaded_by, created_at, updated_at)
        SELECT DISTINCT ON (cif.file_url)
               gen_random_uuid(), ci.organization_id, ci.store_id, cif.file_url, cif.file_type,
               'active', cif.uploaded_by, cif.created_at, cif.created_at
        FROM cl_item_files cif
        LEFT JOIN cl_instance_items cii ON cii.id = cif.item_id
        LEFT JOIN cl_instances ci ON ci.id = cii.instance_id
        ORDER BY cif.file_url, cif.created_at
        """
    )

    # ── backfill file_usages: cl_item_files 1행 → usage 1행 ───
    # path 가 UNIQUE 라 file_url → files 행이 정확히 1:1 매칭.
    op.execute(
        """
        INSERT INTO file_usages (id, file_id, owner_type, owner_id, context, context_id, sort_order, created_at)
        SELECT gen_random_uuid(), f.id, 'cl_item', cif.item_id, cif.context, cif.context_id, cif.sort_order, cif.created_at
        FROM cl_item_files cif
        JOIN files f ON f.path = cif.file_url
        """
    )

    # ── 구 테이블 폐기 ────────────────────────────────────────
    op.drop_table('cl_item_files')


def downgrade() -> None:
    # cl_item_files 재생성 (base 스키마: file_id 없음, file_url 보유).
    op.create_table(
        'cl_item_files',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('item_id', sa.Uuid(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=True),
        sa.Column('context', sa.String(length=20), nullable=False),
        sa.Column('context_id', sa.Uuid(), nullable=True),
        sa.Column('file_url', sa.String(length=500), nullable=False),
        sa.Column('file_type', sa.String(length=20), nullable=False),
        sa.Column('uploaded_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['cl_instance_items.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cl_item_files_item_id', 'cl_item_files', ['item_id'], unique=False)
    op.create_index('ix_cl_item_files_context', 'cl_item_files', ['context', 'context_id'], unique=False)

    # file_usages(+files) → cl_item_files 복원 (checklist usage 만).
    op.execute(
        """
        INSERT INTO cl_item_files (id, item_id, sort_order, context, context_id, file_url, file_type, uploaded_by, created_at)
        SELECT gen_random_uuid(), u.owner_id, u.sort_order, COALESCE(u.context, 'submission'), u.context_id,
               f.path, f.file_type, f.uploaded_by, u.created_at
        FROM file_usages u
        JOIN files f ON f.id = u.file_id
        WHERE u.owner_type = 'cl_item'
        """
    )

    op.drop_index('ix_file_usages_context', table_name='file_usages')
    op.drop_index('ix_file_usages_file_id', table_name='file_usages')
    op.drop_index('ix_file_usages_owner', table_name='file_usages')
    op.drop_table('file_usages')
    op.drop_index('ix_files_uploaded_by', table_name='files')
    op.drop_index('ix_files_status', table_name='files')
    op.drop_index('ix_files_org_store', table_name='files')
    op.drop_index('ix_files_path', table_name='files')
    op.drop_table('files')
