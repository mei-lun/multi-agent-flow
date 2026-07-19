-- TASK-033: projects 与 project_members 表
--
-- 业务库 maf.db 的初始迁移，建立项目聚合根与项目成员关系表。
-- 对应《多 Agent 协同工具系统设计文档》7.1 节与 TASK-033 任务文档：
--   - projects: 项目聚合根，含软删除（deleted_at）与乐观锁版本（version_no）
--   - project_members: 项目成员关系，(project_id, user_id) 为主键，含角色与版本
--
-- 物理类型映射遵循设计文档 §7：
--   uuid → TEXT、json → TEXT、datetime → TEXT(RFC3339)、bigint → INTEGER
--
-- 事务边界：本脚本禁止包含 BEGIN/COMMIT/ROLLBACK，由 MigrationRunner 统一管理。

CREATE TABLE IF NOT EXISTS projects (
    id              TEXT    PRIMARY KEY,
    name            TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'ACTIVE',
    created_at      TEXT    NOT NULL,
    created_by      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    version_no      INTEGER NOT NULL DEFAULT 1,
    deleted_at      TEXT,
    CHECK (status IN ('ACTIVE', 'ARCHIVED'))
);

CREATE INDEX IF NOT EXISTS idx_projects_created_by ON projects(created_by);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);

CREATE TABLE IF NOT EXISTS project_members (
    project_id      TEXT    NOT NULL,
    user_id         TEXT    NOT NULL,
    role            TEXT    NOT NULL,
    added_at        TEXT    NOT NULL,
    added_by        TEXT    NOT NULL,
    version_no      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, user_id),
    CHECK (role IN ('OWNER', 'APPROVER', 'OBSERVER', 'DESIGNER'))
);

CREATE INDEX IF NOT EXISTS idx_project_members_user_id ON project_members(user_id);
CREATE INDEX IF NOT EXISTS idx_project_members_role ON project_members(project_id, role);
