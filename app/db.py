"""SQLite 存储层：连接管理、建表、写事务。

所有时间统一存为 UTC ISO8601 字符串（如 2026-09-30T08:00:00+00:00），
格式一致时可直接按字符串比较大小。
"""
import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS material_batches (
    batch_id    TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL          -- 首次登记时间（UTC），用于“投入前可用”判定
);

-- 原料过敏原声明：每次登记/更正追加一个版本，当前有效版本 = 最大 version
CREATE TABLE IF NOT EXISTS material_declarations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL REFERENCES material_batches(batch_id),
    version     INTEGER NOT NULL,
    allergens   TEXT NOT NULL,         -- JSON 数组，如 ["milk","peanut"]
    pending     INTEGER NOT NULL,      -- 1 = 待确认
    declared_at TEXT NOT NULL,
    UNIQUE (batch_id, version)
);

CREATE TABLE IF NOT EXISTS processings (
    processing_id TEXT PRIMARY KEY,
    equipment_id  TEXT NOT NULL,
    start_utc     TEXT NOT NULL,
    end_utc       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processing_inputs (
    processing_id TEXT NOT NULL REFERENCES processings(processing_id),
    batch_id      TEXT NOT NULL,
    PRIMARY KEY (processing_id, batch_id)
);

-- 一个批次最多只能被加工产出一次（batch_id 唯一）
CREATE TABLE IF NOT EXISTS processing_outputs (
    processing_id TEXT NOT NULL REFERENCES processings(processing_id),
    batch_id      TEXT NOT NULL UNIQUE,
    PRIMARY KEY (processing_id, batch_id)
);

CREATE TABLE IF NOT EXISTS cleanings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id TEXT NOT NULL,
    cleaned_at   TEXT NOT NULL,
    verified     INTEGER NOT NULL,     -- 是否已验证
    revoked      INTEGER NOT NULL DEFAULT 0  -- 1 = 已撤销，撤销后不再清除残留
);

-- 已签发标签：snapshot 为签发时刻冻结的结论与依据（JSON），不可更改
CREATE TABLE IF NOT EXISTS labels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL,
    issued_at     TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES labels(id),
    snapshot      TEXT NOT NULL
);

-- 调查封存：QC 以“唯一调查号 + 疑似批次 + 原因”登记；受影响批次沿加工投入链
-- 与同设备相邻加工残留链（有效清洗截断）实时传播，与过敏原声明是否为空无关。
-- status: open（生效中）/ lifted（已解除；解除只移除该调查自身的影响）。
CREATE TABLE IF NOT EXISTS investigations (
    investigation_id TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL,          -- 疑似交叉接触的根批次
    reason      TEXT NOT NULL,
    opened_at   TEXT NOT NULL,
    lifted_at   TEXT,                  -- NULL = 仍生效
    status      TEXT NOT NULL DEFAULT 'open'
);

-- 过敏原代码并码（同义代码合并）：质控登记 old_code -> target_code，每个旧代码
-- 同一时刻只能有一条**生效（revoked=0）**并码（部分 UNIQUE 索引强约束）；
-- target_code 后续自身也可被并码（串接），解析时沿生效关系链到最终规范代码。
-- 质控发现登记有误时可凭并码记录号 + 必填原因撤销（revoked=1，并留撤销原因/
-- 时间）；撤销后该旧代码可重新并入“当前非别名”的目标，新登记另起记录，
-- 原撤销记录保留可查。原料声明版本与标签快照中的原始代码永不改写出库，
-- 只在实时计算时按本表**仍生效**的关系规范化。
CREATE TABLE IF NOT EXISTS allergen_code_merges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    old_code    TEXT NOT NULL,
    target_code TEXT NOT NULL,
    reason      TEXT NOT NULL,          -- 必填并码原因
    merged_at   TEXT NOT NULL,
    revoked          INTEGER NOT NULL DEFAULT 0,  -- 1 = 已撤销（关系不再生效，记录保留）
    revoke_reason    TEXT,                          -- 必填撤销原因
    revoked_at       TEXT                           -- 撤销时间（UTC）
);

-- 仅对仍生效的关系限定 old_code 唯一；撤销后该旧代码允许重新并码。
CREATE UNIQUE INDEX IF NOT EXISTS idx_allergen_merges_active_old
    ON allergen_code_merges(old_code) WHERE revoked = 0;

-- 变更事件流：与业务写入同一事务落库，下游按游标（seq）增量消费。
-- seq 全局递增；同一次操作产生的多条事件按批次号排序（插入序即批次号序）。
CREATE TABLE IF NOT EXISTS trace_events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,  -- 全局递增序号
    op          TEXT NOT NULL,       -- 触发操作类型（登记/更正/撤销/预演提交等）
    op_ref      TEXT NOT NULL,       -- JSON：操作定位信息（加工号/清洗 id/声明版本/凭据等）
    batch_id    TEXT NOT NULL,       -- 批次号
    kind        TEXT NOT NULL,       -- material / product
    before_view TEXT,                -- JSON：变更前完整结论与依据；新批次为 NULL
    after_view  TEXT NOT NULL,       -- JSON：变更后完整结论与依据
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trace_events_batch ON trace_events(batch_id);
"""

# 旧版 allergen_code_merges 在 old_code 上带表级 UNIQUE（自动索引
# sqlite_autoindex_allergen_code_merges_*）且无撤销列；升级时整表重建：
# 现存关系作为“仍生效”关系迁入，撤销列置空，并以部分唯一索引替代旧约束。
_MERGE_REBUILD_SQL = """
CREATE TABLE allergen_code_merges_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    old_code    TEXT NOT NULL,
    target_code TEXT NOT NULL,
    reason      TEXT NOT NULL,
    merged_at   TEXT NOT NULL,
    revoked          INTEGER NOT NULL DEFAULT 0,
    revoke_reason    TEXT,
    revoked_at       TEXT
);
INSERT INTO allergen_code_merges_new
    (id, old_code, target_code, reason, merged_at, revoked, revoke_reason, revoked_at)
SELECT id, old_code, target_code, reason, merged_at, 0, NULL, NULL
FROM allergen_code_merges;
DROP TABLE allergen_code_merges;
ALTER TABLE allergen_code_merges_new RENAME TO allergen_code_merges;
CREATE UNIQUE INDEX idx_allergen_merges_active_old
    ON allergen_code_merges(old_code) WHERE revoked = 0;
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """一次性结构迁移：把旧版（old_code 表级 UNIQUE、无撤销列）并码表升级为
    可撤销/可重新并码的新结构。新库无需迁移。"""
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(allergen_code_merges)").fetchall()}
    if not cols or "revoked" in cols:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executescript(_MERGE_REBUILD_SQL)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def db_path() -> str:
    return os.environ.get("DB_PATH", "app.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        # 先迁移旧版并码表（SCHEMA 中的部分唯一索引依赖新列，须在执行
        # SCHEMA 建索引前完成整表重建），再幂等建表/索引。
        _migrate(conn)
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """写事务：BEGIN IMMEDIATE 保证“校验+写入”原子，失败回滚保持原状态。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    conn.commit()


@contextmanager
def read_tx(conn: sqlite3.Connection):
    """读事务：WAL 下为多个查询提供一致快照（初始化读取/分页读取看到同一状态），
    不阻塞并发写入。"""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()  # 只读事务，结束即释放（等效提交）
