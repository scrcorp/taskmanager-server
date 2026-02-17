-- ============================================
-- Employee Management Service - MVP DB Schema v3
-- PostgreSQL
-- ============================================
-- v2 → v3 변경사항:
--   1. email_verifications 제거 (MVP에서 이메일 인증 생략)
--   2. notifications 테이블 추가 (인앱 알림)
--   3. feedbacks, feedback_media → Phase 4 표기 (스키마에 유지, MVP에서 미구현)

-- ============================================
-- 1. MULTI-TENANT & ORGANIZATION
-- ============================================

CREATE TABLE organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE brands (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    address TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- 2. ROLES & USERS
-- ============================================

CREATE TABLE roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name VARCHAR(50) NOT NULL,
    level INT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(organization_id, name)
);

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    role_id UUID NOT NULL REFERENCES roles(id),
    username VARCHAR(100) NOT NULL,
    email VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    is_active BOOLEAN DEFAULT TRUE,
    email_verified BOOLEAN DEFAULT FALSE,   -- 추후 이메일 인증 시 활용
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(organization_id, username),
    UNIQUE(organization_id, email)
);

CREATE TABLE refresh_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token VARCHAR(500) NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE user_brands (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    brand_id UUID NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, brand_id)
);

-- ============================================
-- 3. WORK STRUCTURE (브랜드 종속)
-- ============================================

CREATE TABLE shifts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    sort_order INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(brand_id, name)
);

CREATE TABLE positions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    sort_order INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(brand_id, name)
);

-- ============================================
-- 4. CHECKLIST TEMPLATES
-- ============================================

CREATE TABLE checklist_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    shift_id UUID NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
    position_id UUID NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(brand_id, shift_id, position_id)
);

-- verification_type: 'none' (MVP) | 'photo' | 'text' (Phase 4)
CREATE TABLE checklist_template_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    checklist_template_id UUID NOT NULL REFERENCES checklist_templates(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    verification_type VARCHAR(20) NOT NULL DEFAULT 'none',
    sort_order INT DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT chk_verification_type CHECK (verification_type IN ('none', 'photo', 'text'))
);

-- ============================================
-- 5. WORK ASSIGNMENTS + CHECKLIST SNAPSHOT
-- ============================================

-- checklist_snapshot JSONB 구조:
-- {
--   "template_id": "uuid",
--   "template_name": "A브랜드-오픈-주방",
--   "snapshot_at": "2025-02-15T00:00:00Z",
--   "items": [
--     {
--       "template_item_id": "uuid",
--       "title": "그릴 예열",
--       "description": null,
--       "verification_type": "none",
--       "sort_order": 1,
--       "is_completed": false,
--       "completed_at": null,
--       "verification_data": null
--     }
--   ]
-- }

CREATE TABLE work_assignments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    shift_id UUID NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
    position_id UUID NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assigned_by UUID NOT NULL REFERENCES users(id),
    work_date DATE NOT NULL,
    status VARCHAR(20) DEFAULT 'assigned',
    checklist_snapshot JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- 6. ADDITIONAL TASKS
-- ============================================

CREATE TABLE additional_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    brand_id UUID REFERENCES brands(id) ON DELETE SET NULL,
    created_by UUID NOT NULL REFERENCES users(id),
    title VARCHAR(255) NOT NULL,
    description TEXT,
    priority VARCHAR(20) DEFAULT 'normal',
    status VARCHAR(20) DEFAULT 'pending',
    due_date TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE additional_task_assignees (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    additional_task_id UUID NOT NULL REFERENCES additional_tasks(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    is_completed BOOLEAN DEFAULT FALSE,
    completed_at TIMESTAMPTZ,
    UNIQUE(additional_task_id, user_id)
);

-- ============================================
-- 7. ANNOUNCEMENTS
-- ============================================

CREATE TABLE announcements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    brand_id UUID REFERENCES brands(id) ON DELETE SET NULL,
    created_by UUID NOT NULL REFERENCES users(id),
    title VARCHAR(255) NOT NULL,
    content TEXT NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- 8. NOTIFICATIONS (인앱 알림)
-- ============================================

CREATE TABLE notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type VARCHAR(50) NOT NULL,              -- work_assigned, additional_task, announcement, etc.
    title VARCHAR(255) NOT NULL,
    message TEXT,
    reference_type VARCHAR(50),             -- work_assignment, additional_task, announcement
    reference_id UUID,                      -- 관련 엔티티 ID
    is_read BOOLEAN DEFAULT FALSE,
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- 9. MEDIA (Phase 4)
-- ============================================

CREATE TABLE media (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uploaded_by UUID NOT NULL REFERENCES users(id),
    file_url TEXT NOT NULL,
    file_type VARCHAR(20) NOT NULL,
    file_size BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- 10. FEEDBACK (Phase 4)
-- ============================================

CREATE TABLE feedbacks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    work_assignment_id UUID REFERENCES work_assignments(id) ON DELETE SET NULL,
    from_user_id UUID NOT NULL REFERENCES users(id),
    to_user_id UUID NOT NULL REFERENCES users(id),
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE feedback_media (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    feedback_id UUID NOT NULL REFERENCES feedbacks(id) ON DELETE CASCADE,
    media_id UUID NOT NULL REFERENCES media(id) ON DELETE CASCADE
);

-- ============================================
-- 11. EMAIL VERIFICATION (Phase 4)
-- ============================================

CREATE TABLE email_verifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) NOT NULL,
    code VARCHAR(6) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- INDEXES
-- ============================================

-- Users
CREATE INDEX idx_users_org ON users(organization_id);
CREATE INDEX idx_users_role ON users(role_id);

-- Brands
CREATE INDEX idx_brands_org ON brands(organization_id);

-- User-Brand
CREATE INDEX idx_user_brands_user ON user_brands(user_id);
CREATE INDEX idx_user_brands_brand ON user_brands(brand_id);

-- Shifts & Positions
CREATE INDEX idx_shifts_brand ON shifts(brand_id);
CREATE INDEX idx_positions_brand ON positions(brand_id);

-- Work Assignments
CREATE INDEX idx_work_assignments_user_date ON work_assignments(user_id, work_date);
CREATE INDEX idx_work_assignments_brand_date ON work_assignments(brand_id, work_date);
CREATE INDEX idx_work_assignments_date ON work_assignments(work_date);

-- Additional Tasks
CREATE INDEX idx_additional_tasks_org ON additional_tasks(organization_id);
CREATE INDEX idx_additional_task_assignees_user ON additional_task_assignees(user_id);

-- Announcements
CREATE INDEX idx_announcements_org ON announcements(organization_id);
CREATE INDEX idx_announcements_brand ON announcements(brand_id);

-- Notifications
CREATE INDEX idx_notifications_user ON notifications(user_id);
CREATE INDEX idx_notifications_user_unread ON notifications(user_id, is_read) WHERE is_read = FALSE;
CREATE INDEX idx_notifications_created ON notifications(created_at DESC);

-- Refresh Tokens
CREATE INDEX idx_refresh_tokens_user ON refresh_tokens(user_id);

-- Phase 4 Indexes
CREATE INDEX idx_feedbacks_to_user ON feedbacks(to_user_id);
CREATE INDEX idx_feedbacks_assignment ON feedbacks(work_assignment_id);
