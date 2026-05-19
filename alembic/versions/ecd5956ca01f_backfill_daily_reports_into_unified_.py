"""backfill daily reports into unified reports

Revision ID: ecd5956ca01f
Revises: 989f0fa82df1
Create Date: 2026-05-11 15:24:28.442435

기존 daily_reports / daily_report_sections / daily_report_comments /
daily_report_templates / daily_report_template_sections 데이터를
새 reports / report_comments / report_templates 로 type='daily' 로 복사.

- ID 그대로 보존 (FK 참조 호환 + 멱등성 확보 via ON CONFLICT DO NOTHING)
- daily 본문 sections는 reports.payload.sections JSONB 배열로 직렬화
- daily 템플릿 sections는 report_templates.payload.sections JSONB 배열로 직렬화
- daily_reports.period → reports.payload.period

기존 daily_reports* 테이블은 그대로 둠. 별도 PR에서 검증 후 제거.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'ecd5956ca01f'
down_revision: Union[str, None] = '989f0fa82df1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. report_templates: daily_report_templates + sections → type='daily'
    op.execute("""
        INSERT INTO report_templates (
            id, type, organization_id, store_id, name,
            is_default, is_active, payload, created_at, updated_at
        )
        SELECT
            t.id,
            'daily',
            t.organization_id,
            t.store_id,
            t.name,
            t.is_default,
            t.is_active,
            jsonb_build_object(
                'sections', COALESCE((
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'id', s.id,
                            'title', s.title,
                            'description', s.description,
                            'is_required', s.is_required,
                            'sort_order', s.sort_order
                        ) ORDER BY s.sort_order
                    )
                    FROM daily_report_template_sections s
                    WHERE s.template_id = t.id
                ), '[]'::jsonb)
            ),
            t.created_at,
            t.updated_at
        FROM daily_report_templates t
        ON CONFLICT (id) DO NOTHING
    """)

    # 2. reports: daily_reports + sections → type='daily'
    op.execute("""
        INSERT INTO reports (
            id, type, organization_id, store_id, template_id, author_id,
            title, status, report_date, submitted_at, deleted_at,
            payload, created_at, updated_at
        )
        SELECT
            r.id,
            'daily',
            r.organization_id,
            r.store_id,
            r.template_id,
            r.author_id,
            NULL,
            r.status,
            r.report_date,
            r.submitted_at,
            r.deleted_at,
            jsonb_build_object(
                'period', r.period,
                'sections', COALESCE((
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'id', s.id,
                            'title', s.title,
                            'content', s.content,
                            'sort_order', s.sort_order,
                            'template_section_id', s.template_section_id
                        ) ORDER BY s.sort_order
                    )
                    FROM daily_report_sections s
                    WHERE s.report_id = r.id
                ), '[]'::jsonb)
            ),
            r.created_at,
            r.updated_at
        FROM daily_reports r
        ON CONFLICT (id) DO NOTHING
    """)

    # 3. report_comments: daily_report_comments → 1:1
    op.execute("""
        INSERT INTO report_comments (id, report_id, user_id, content, created_at)
        SELECT id, report_id, user_id, content, created_at
        FROM daily_report_comments
        ON CONFLICT (id) DO NOTHING
    """)


def downgrade() -> None:
    # daily 타입만 정리 (다른 타입이 생긴 경우 그대로 보존)
    op.execute("DELETE FROM report_comments WHERE report_id IN (SELECT id FROM reports WHERE type = 'daily')")
    op.execute("DELETE FROM reports WHERE type = 'daily'")
    op.execute("DELETE FROM report_templates WHERE type = 'daily'")
