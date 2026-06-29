"""re-backfill daily reports legacy to unified (heal cutover gap)

Revision ID: 316128a85fd9
Revises: 4ae194c4f826
Create Date: 2026-06-29 18:11:00.829980

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '316128a85fd9'
down_revision: Union[str, None] = '4ae194c4f826'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """레거시 daily_reports → 통합 reports 재백필 (cutover 갭 복구).

    원본 백필(ecd5956ca01f)은 2026-05-11에 1회 실행됐고, 이후 클라이언트가
    cutover 전까지 레거시에 계속 써서 통합 테이블에 누락분이 생김(= 사라져 보이는 리포트).
    이 마이그레이션은 누락분만 idempotent하게 복원한다.

    idempotency 2중 가드:
      1) reports.id = daily_reports.id 재사용 → ON CONFLICT (id) DO NOTHING
      2) per-person partial unique index (store, date, period, author) 충돌 방지:
         cutover 후 같은 slot+author로 통합에 새로 쓰인 live 행이 있으면 skip
    레거시 테이블은 읽기만 한다(삭제/변경 없음).
    """
    # 1) reports 재백필 (누락분만)
    op.execute("""
        INSERT INTO reports (
            id, type, organization_id, store_id, template_id, author_id,
            title, status, report_date, submitted_at, deleted_at,
            payload, created_at, updated_at
        )
        SELECT
            r.id, 'daily', r.organization_id, r.store_id, r.template_id, r.author_id,
            NULL, r.status, r.report_date, r.submitted_at, r.deleted_at,
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
            r.created_at, r.updated_at
        FROM daily_reports r
        WHERE NOT EXISTS (
            SELECT 1 FROM reports r2
            WHERE r2.type = 'daily'
              AND r2.deleted_at IS NULL
              AND r2.store_id = r.store_id
              AND r2.report_date = r.report_date
              AND r2.payload->>'period' = r.period
              AND r2.author_id IS NOT DISTINCT FROM r.author_id
        )
        ON CONFLICT (id) DO NOTHING
    """)

    # 2) report_comments 재백필 (위에서 복원된 리포트의 댓글 포함)
    op.execute("""
        INSERT INTO report_comments (id, report_id, user_id, content, created_at)
        SELECT c.id, c.report_id, c.user_id, c.content, c.created_at
        FROM daily_report_comments c
        WHERE EXISTS (SELECT 1 FROM reports r WHERE r.id = c.report_id)
        ON CONFLICT (id) DO NOTHING
    """)


def downgrade() -> None:
    # 데이터 복구(heal) 마이그레이션 — downgrade 시 복원한 사용자 데이터를
    # 식별/삭제하지 않는다(원본과 재백필분 구분 불가, 데이터 유실 위험).
    pass
