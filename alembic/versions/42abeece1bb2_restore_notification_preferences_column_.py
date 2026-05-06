"""restore notification_preferences column and announcement permissions for backwards compat

Revision ID: 42abeece1bb2
Revises: d47d16e25542
Create Date: 2026-05-06 11:26:37.717962

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '42abeece1bb2'
down_revision: Union[str, None] = 'd47d16e25542'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Phase 1 패턴 일관성 회복 — 옛 컬럼/권한을 다시 만들고 새것과 양방향 sync.

    이전 마이그레이션 (5404355df377, d47d16e25542) 이 in-place RENAME / DELETE
    로 옛 컬럼/권한을 없애 옛 API 호출 시 깨졌음. 본 마이그레이션은 옛것을
    다시 만들고 trigger 로 양쪽 동기화. Phase 4 일괄 정리 시 함께 제거.
    """
    # ─────────────────────────────────────────────────────────────────
    # 1. users.notification_preferences 컬럼 복구 + 양방향 sync
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        ALTER TABLE users
        ADD COLUMN notification_preferences JSONB NOT NULL DEFAULT '{}'
    """)

    # alert_preferences → notification_preferences (카테고리 코드 notice → announcement 역변환)
    op.execute("""
        UPDATE users
        SET notification_preferences = (
            CASE
                WHEN alert_preferences ? 'notice' THEN
                    (alert_preferences - 'notice')
                    || jsonb_build_object('announcement', alert_preferences->'notice')
                ELSE alert_preferences
            END
        )
    """)

    # 양방향 sync trigger — 한쪽 UPDATE 시 다른쪽 자동 반영.
    # pg_trigger_depth() 로 무한루프 방지.
    op.execute("""
        CREATE OR REPLACE FUNCTION sync_user_pref_columns() RETURNS trigger AS $$
        BEGIN
            -- 재귀(다른 trigger 가 트리거한 것)면 sync 생략
            IF pg_trigger_depth() > 1 THEN
                RETURN NEW;
            END IF;

            IF NEW.notification_preferences IS DISTINCT FROM OLD.notification_preferences THEN
                -- notification → alert (카테고리 코드 announcement → notice 변환)
                NEW.alert_preferences := (
                    CASE
                        WHEN NEW.notification_preferences ? 'announcement' THEN
                            (NEW.notification_preferences - 'announcement')
                            || jsonb_build_object('notice',
                                NEW.notification_preferences->'announcement')
                        ELSE NEW.notification_preferences
                    END
                );
            ELSIF NEW.alert_preferences IS DISTINCT FROM OLD.alert_preferences THEN
                -- alert → notification (역변환)
                NEW.notification_preferences := (
                    CASE
                        WHEN NEW.alert_preferences ? 'notice' THEN
                            (NEW.alert_preferences - 'notice')
                            || jsonb_build_object('announcement',
                                NEW.alert_preferences->'notice')
                        ELSE NEW.alert_preferences
                    END
                );
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER tr_sync_user_pref_columns
        BEFORE UPDATE ON users
        FOR EACH ROW EXECUTE FUNCTION sync_user_pref_columns()
    """)

    # ─────────────────────────────────────────────────────────────────
    # 2. permissions 테이블에 announcements:* 4개 복구
    #    + role_permissions 에서 notices:* 매핑된 role 들에 announcements:* 도 매핑
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        INSERT INTO permissions (id, code, resource, action, description,
                                 require_priority_check, created_at)
        SELECT gen_random_uuid(),
               'announcements:' || action,
               'announcements',
               action,
               description,
               require_priority_check,
               NOW()
        FROM permissions
        WHERE code LIKE 'notices:%'
        ON CONFLICT (code) DO NOTHING
    """)

    op.execute("""
        INSERT INTO role_permissions (id, role_id, permission_id, created_at)
        SELECT gen_random_uuid(), rp.role_id, p_old.id, NOW()
        FROM role_permissions rp
        JOIN permissions p_new ON rp.permission_id = p_new.id
        JOIN permissions p_old
             ON p_old.code = REPLACE(p_new.code, 'notices:', 'announcements:')
        WHERE p_new.code LIKE 'notices:%'
          AND p_old.code LIKE 'announcements:%'
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tr_sync_user_pref_columns ON users")
    op.execute("DROP FUNCTION IF EXISTS sync_user_pref_columns()")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS notification_preferences")
    op.execute("""
        DELETE FROM role_permissions
        WHERE permission_id IN (
            SELECT id FROM permissions WHERE code LIKE 'announcements:%'
        )
    """)
    op.execute("DELETE FROM permissions WHERE code LIKE 'announcements:%'")
