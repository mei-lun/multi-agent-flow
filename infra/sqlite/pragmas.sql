-- SQLite 基线 PRAGMA 配置
-- 来源：《多 Agent 协同工具系统设计文档》6.0 节
-- 应用范围：maf.db（业务投影）与 checkpoints.db（LangGraph checkpoint）
-- 应用时机：server 启动时对每个新连接执行；WAL 为持久 PRAGMA（存于数据库头），
--   其余为 per-connection，由 core/database.py 在每次打开连接时重新应用。
-- 约束：只有 Server 进程可写这两个数据库；SQLite 是 Git control 分支的可重建投影。

-- WAL 模式：提高同机读写并发；仍只有一个 writer。
PRAGMA journal_mode = WAL;

-- 外键约束：业务表关系完整性。
PRAGMA foreign_keys = ON;

-- 锁等待：写入遇到锁时等待 5000 毫秒，避免立即 "database is locked"。
PRAGMA busy_timeout = 5000;

-- 同步级别：NORMAL 在 WAL 下保证事务耐久性且减少 fsync 次数。
PRAGMA synchronous = NORMAL;

-- 临时表与中间结果存内存，减少磁盘 I/O。
PRAGMA temp_store = MEMORY;
