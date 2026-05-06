"""create alerts/notices/notice_reads + backfill + sync triggers

Revision ID: a6162a07617c
Revises: f426c3548271
Create Date: 2026-05-06

Phase 1 of notifications→alerts / announcements→notices rename.

전략:
1. 새 테이블 3개 create (alerts/notices/notice_reads), 기존과 schema 동일
   (notice_reads.announcement_id 만 notice_id 로 컬럼명 변경)
2. 기존 데이터 backfill — 한 번 복사
3. trigger 3개 — 기존 테이블 INSERT/UPDATE/DELETE 시 새 테이블에 자동 sync
   (단방향, Phase 4 직전까지 유지)

기존 코드/API/모델은 손대지 않는다 — 알림 설정 PR 의 notification_preferences
컬럼도 그대로. Phase 2/3 에서 새 모델/API 추가 후 클라가 새 endpoint 호출하면
서 정리.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a6162a07617c'
down_revision: Union[str, None] = '8a711461e92d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─────────────────────────────────────────────────────────────────
    # 1. alerts — notifications 와 동일 schema
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE alerts (
            id              UUID PRIMARY KEY,
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            type            VARCHAR(50) NOT NULL,
            message         VARCHAR(1000) NOT NULL,
            reference_type  VARCHAR(50),
            reference_id    UUID,
            is_read         BOOLEAN NOT NULL DEFAULT FALSE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_alerts_user_unread ON alerts (user_id, is_read)")
    op.execute("CREATE INDEX ix_alerts_user_created ON alerts (user_id, created_at DESC)")

    # ─────────────────────────────────────────────────────────────────
    # 2. notices — announcements 와 동일 schema
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE notices (
            id              UUID PRIMARY KEY,
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            store_id        UUID REFERENCES stores(id) ON DELETE SET NULL,
            title           VARCHAR(500) NOT NULL,
            content         TEXT NOT NULL,
            created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
            deleted_at      TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_notices_org_created ON notices (organization_id, created_at DESC)")
    op.execute("CREATE INDEX ix_notices_store ON notices (store_id) WHERE store_id IS NOT NULL")

    # ─────────────────────────────────────────────────────────────────
    # 3. notice_reads — announcement_reads 와 동일하나 컬럼명 변경
    #    (announcement_id → notice_id)
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE notice_reads (
            id        UUID PRIMARY KEY,
            notice_id UUID NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
            user_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            read_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_notice_read UNIQUE (notice_id, user_id)
        )
    """)

    # ─────────────────────────────────────────────────────────────────
    # 4. Backfill — 한 번 복사 (이후는 trigger 가 sync)
    # ─────────────────────────────────────────────────────────────────
    op.execute("""
        INSERT INTO alerts (id, organization_id, user_id, type, message,
                            reference_type, reference_id, is_read, created_at)
        SELECT id, organization_id, user_id, type, message,
               reference_type, reference_id, is_read, created_at
        FROM notifications
        ON CONFLICT (id) DO NOTHING
    """)
    op.execute("""
        INSERT INTO notices (id, organization_id, store_id, title, content,
                             created_by, deleted_at, created_at, updated_at)
        SELECT id, organization_id, store_id, title, content,
               created_by, deleted_at, created_at, updated_at
        FROM announcements
        ON CONFLICT (id) DO NOTHING
    """)
    op.execute("""
        INSERT INTO notice_reads (id, notice_id, user_id, read_at)
        SELECT id, announcement_id, user_id, read_at
        FROM announcement_reads
        ON CONFLICT (id) DO NOTHING
    """)

    # ─────────────────────────────────────────────────────────────────
    # 5. Trigger functions — 기존 → 새 테이블 sync
    #    단방향이라 pg_trigger_depth() 체크 불필요. Phase 4 직전 양방향
    #    필요 시 추가.
    # ─────────────────────────────────────────────────────────────────

    # 5a. notifications → alerts
    op.execute("""
        CREATE OR REPLACE FUNCTION sync_notification_to_alert() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                INSERT INTO alerts (id, organization_id, user_id, type, message,
                                    reference_type, reference_id, is_read, created_at)
                VALUES (NEW.id, NEW.organization_id, NEW.user_id, NEW.type, NEW.message,
                        NEW.reference_type, NEW.reference_id, NEW.is_read, NEW.created_at)
                ON CONFLICT (id) DO NOTHING;
                RETURN NEW;
            ELSIF TG_OP = 'UPDATE' THEN
                UPDATE alerts SET
                    organization_id = NEW.organization_id,
                    user_id = NEW.user_id,
                    type = NEW.type,
                    message = NEW.message,
                    reference_type = NEW.reference_type,
                    reference_id = NEW.reference_id,
                    is_read = NEW.is_read,
                    created_at = NEW.created_at
                WHERE id = NEW.id;
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                DELETE FROM alerts WHERE id = OLD.id;
                RETURN OLD;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER tr_sync_notification
        AFTER INSERT OR UPDATE OR DELETE ON notifications
        FOR EACH ROW EXECUTE FUNCTION sync_notification_to_alert()
    """)

    # 5b. announcements → notices
    op.execute("""
        CREATE OR REPLACE FUNCTION sync_announcement_to_notice() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                INSERT INTO notices (id, organization_id, store_id, title, content,
                                     created_by, deleted_at, created_at, updated_at)
                VALUES (NEW.id, NEW.organization_id, NEW.store_id, NEW.title, NEW.content,
                        NEW.created_by, NEW.deleted_at, NEW.created_at, NEW.updated_at)
                ON CONFLICT (id) DO NOTHING;
                RETURN NEW;
            ELSIF TG_OP = 'UPDATE' THEN
                UPDATE notices SET
                    organization_id = NEW.organization_id,
                    store_id = NEW.store_id,
                    title = NEW.title,
                    content = NEW.content,
                    created_by = NEW.created_by,
                    deleted_at = NEW.deleted_at,
                    created_at = NEW.created_at,
                    updated_at = NEW.updated_at
                WHERE id = NEW.id;
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                DELETE FROM notices WHERE id = OLD.id;
                RETURN OLD;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER tr_sync_announcement
        AFTER INSERT OR UPDATE OR DELETE ON announcements
        FOR EACH ROW EXECUTE FUNCTION sync_announcement_to_notice()
    """)

    # 5c. announcement_reads → notice_reads (column rename: announcement_id → notice_id)
    op.execute("""
        CREATE OR REPLACE FUNCTION sync_announcement_read_to_notice_read() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                INSERT INTO notice_reads (id, notice_id, user_id, read_at)
                VALUES (NEW.id, NEW.announcement_id, NEW.user_id, NEW.read_at)
                ON CONFLICT (id) DO NOTHING;
                RETURN NEW;
            ELSIF TG_OP = 'UPDATE' THEN
                UPDATE notice_reads SET
                    notice_id = NEW.announcement_id,
                    user_id = NEW.user_id,
                    read_at = NEW.read_at
                WHERE id = NEW.id;
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                DELETE FROM notice_reads WHERE id = OLD.id;
                RETURN OLD;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER tr_sync_announcement_read
        AFTER INSERT OR UPDATE OR DELETE ON announcement_reads
        FOR EACH ROW EXECUTE FUNCTION sync_announcement_read_to_notice_read()
    """)


def downgrade() -> None:
    # Triggers
    op.execute("DROP TRIGGER IF EXISTS tr_sync_announcement_read ON announcement_reads")
    op.execute("DROP TRIGGER IF EXISTS tr_sync_announcement ON announcements")
    op.execute("DROP TRIGGER IF EXISTS tr_sync_notification ON notifications")
    op.execute("DROP FUNCTION IF EXISTS sync_announcement_read_to_notice_read()")
    op.execute("DROP FUNCTION IF EXISTS sync_announcement_to_notice()")
    op.execute("DROP FUNCTION IF EXISTS sync_notification_to_alert()")

    # Tables (FK 순서대로)
    op.execute("DROP TABLE IF EXISTS notice_reads")
    op.execute("DROP TABLE IF EXISTS notices")
    op.execute("DROP TABLE IF EXISTS alerts")
