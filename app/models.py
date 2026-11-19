"""SQLite 建表与连接。单文件库，备份 = 复制 data/museum.db。

性能设计（数据量增长也不怕）：
- 结构化筛选列全部有索引（品类/系列/机构/券种/印刷厂/文书/朝代/评级公司/年份/状态）
- 自由文本搜索走 FTS5 trigram 中文全文索引（3 字以上子串检索，十万级毫秒响应）；
  不足 3 字自动降级 LIKE（小库无感知，大库只影响短词搜索）
- 未来单库超过 ~50 万条再考虑迁 PostgreSQL（字段设计已兼容）
"""
import sqlite3
from typing import Optional

from .config import DB_PATH

SCHEMA = """
-- 字典表：下拉选项与筛选维度的数据源，后台可增改，不用改代码
CREATE TABLE IF NOT EXISTS dict_entry (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    type    TEXT NOT NULL,   -- category/dynasty/bank/note_type/printer/doc_type/grade_co/material/cert_domain
    name    TEXT NOT NULL,   -- 显示名，如 中央银行
    aliases TEXT,            -- 别名(逗号分隔)：英文行名、缩写，搜索与标签映射用
    sort    INTEGER DEFAULT 0,
    UNIQUE(type, name)
);

-- 多用户：admin=全部权限；staff=录入（看不到价格）；viewer=只读（看不到价格）
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,            -- pbkdf2_sha256，格式 salt$hash
    role          TEXT NOT NULL DEFAULT 'staff',
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS item (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_code       TEXT UNIQUE,                -- 内部编号 M-2026-0001
    category        TEXT,                       -- 品类（字典 category）
    dynasty         TEXT,                       -- 朝代（有序字典，古钱用）
    bank            TEXT,                       -- 发行机构（纸币用，如 中央银行）
    note_type       TEXT,                       -- 券种：法币/金圆券/银元券/关金券…
    series          TEXT,                       -- 时代系列：明清票券/边区·解放区/第一套人民币…
    printer         TEXT,                       -- 印刷厂，如 美国钞票公司
    doc_type        TEXT,                       -- 文书类型（契约票证类）：地契/当票/老股票…
    name            TEXT,                       -- 显示名，如 中央银行 金圆券 壹万圆
    era             TEXT,                       -- 年号/品种（古钱用，如 康熙通宝）
    denomination    TEXT,                       -- 面值：壹圆/当十/壹万圆
    variety         TEXT,                       -- 版别（自由文本+标签起步）
    material        TEXT,
    size_mm         REAL,
    weight_g        REAL,
    year_text       TEXT,                       -- 原文纪年：民国十九年 / 咸丰三年
    year_sort       INTEGER,                    -- 换算后的排序年（民国十九年→1930；古钱可空）
    region          TEXT,                       -- 地域（地契等按省/县收藏，如 山西·平遥）
    grade_co        TEXT,                       -- 评级公司
    cert_no         TEXT UNIQUE,                -- 评级编号，唯一键：防重+防假盒
    cert_url        TEXT,                       -- 证书页 URL，来源可追溯
    grade_raw       TEXT,                       -- 分数原文，如 MS 63 / 美品 78
    grade_scale     TEXT,                       -- 分数制：70制/85制/描述
    serial_no       TEXT,                       -- 纸币冠字号
    status          TEXT NOT NULL DEFAULT 'draft',  -- draft/in_stock/reviewing/exhibited/lent/consigned/sold/lost
    location_id     INTEGER REFERENCES location(id),
    source          TEXT,                       -- 购入/捐赠/代管/交换
    purchase_price  REAL,
    purchase_date   TEXT,
    est_value       REAL,
    est_date        TEXT,
    level           TEXT,                       -- 藏品等级（镇馆之宝/一级/二级/三级/一般）
    catalog_ref     TEXT,                       -- 目录号：Pick#/KM#/泉谱编号（收藏行业标准）
    condition_st    TEXT,                       -- 保存状况：完好/轻微氧化/需处理/已修复
    intro_text      TEXT,                       -- 讲解介绍文案（AI生成可编辑，导览页展示）
    intro_audio     TEXT,                       -- 30秒讲解语音（相对路径，TTS生成）
    pub_views       INTEGER DEFAULT 0,          -- 导览页浏览次数（运营数据）
    send_express    TEXT,
    send_fee        REAL,
    notes           TEXT,
    created_by      TEXT,
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_item_category  ON item(category);
CREATE INDEX IF NOT EXISTS idx_item_bank      ON item(bank);
CREATE INDEX IF NOT EXISTS idx_item_note_type ON item(note_type);
CREATE INDEX IF NOT EXISTS idx_item_series    ON item(series);
CREATE INDEX IF NOT EXISTS idx_item_printer   ON item(printer);
CREATE INDEX IF NOT EXISTS idx_item_doc_type  ON item(doc_type);
CREATE INDEX IF NOT EXISTS idx_item_dynasty   ON item(dynasty);
CREATE INDEX IF NOT EXISTS idx_item_grade_co  ON item(grade_co);
CREATE INDEX IF NOT EXISTS idx_item_year      ON item(year_sort);
CREATE INDEX IF NOT EXISTS idx_item_status    ON item(status);
CREATE INDEX IF NOT EXISTS idx_item_location  ON item(location_id);

CREATE TABLE IF NOT EXISTS photo (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES item(id),
    shot_type   TEXT,                           -- front/back/edge/label/holder/detail/reference
    origin      TEXT DEFAULT 'archive',         -- archive=自拍档案图 / reference=官网参考图
    source_url  TEXT,
    fetched_at  TEXT,
    file_path   TEXT NOT NULL,
    exif_time   TEXT,
    phash       TEXT
);
CREATE INDEX IF NOT EXISTS idx_photo_item  ON photo(item_id);
CREATE INDEX IF NOT EXISTS idx_photo_phash ON photo(phash);

CREATE TABLE IF NOT EXISTS cert_fetch (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER,
    adapter     TEXT,
    url         TEXT,
    ok          INTEGER,
    message     TEXT,
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS recognition (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER,
    engine      TEXT,                           -- qr_cert / ocr_label / llm_coin / manual
    raw_json    TEXT,
    confidence  REAL,
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS location (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES location(id),  -- 树形：展柜→层→盒
    name      TEXT,
    code      TEXT UNIQUE,
    grid_x    INTEGER,                          -- 场馆平面图格子坐标（列）
    grid_y    INTEGER,                          -- 场馆平面图格子坐标（行）
    grid_w    INTEGER DEFAULT 1,                -- 占几格宽（横柜=2）
    grid_h    INTEGER DEFAULT 1                 -- 占几格高（竖柜=2）
);

CREATE TABLE IF NOT EXISTS event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL REFERENCES item(id),
    kind       TEXT,                            -- 入库/状态变更/送评/借出/归还/出售/估值变更
    detail     TEXT,
    operator   TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_event_item ON event(item_id);

-- 站点设置（标题/LOGO 等自定义项）
CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 凭证附件（购入凭证/转让协议/收据——来源合法性留档，仅录入及以上可见）
CREATE TABLE IF NOT EXISTS attachment (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL REFERENCES item(id),
    category   TEXT NOT NULL DEFAULT '其他',    -- 发票/收据/转让协议/证书/其他
    file_path  TEXT NOT NULL,
    notes      TEXT,
    created_by TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_att_item ON attachment(item_id);

-- 估值历史（每次估值变更留痕：手动/AI参考）
CREATE TABLE IF NOT EXISTS value_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL REFERENCES item(id),
    old_value  REAL,
    new_value  REAL,
    source     TEXT,                            -- 手动 / AI参考
    operator   TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_vhist_item ON value_history(item_id);

-- 批量识别任务：照片排队，后台线程限速逐张跑（人可以先走）
CREATE TABLE IF NOT EXISTS batch_job (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by TEXT,
    total      INTEGER NOT NULL DEFAULT 0,
    done       INTEGER NOT NULL DEFAULT 0,
    ok         INTEGER NOT NULL DEFAULT 0,
    dup        INTEGER NOT NULL DEFAULT 0,
    fail       INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'running',   -- running/done
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS batch_item (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   INTEGER NOT NULL REFERENCES batch_job(id),
    filename TEXT,
    path     TEXT,
    status   TEXT NOT NULL DEFAULT 'pending',     -- pending/ok/dup/fail
    item_id  INTEGER,
    message  TEXT
);
CREATE INDEX IF NOT EXISTS idx_batch_item_job ON batch_item(job_id, status);
"""

# 全文检索表（trigram 分词，中文子串友好）。建不出来（老 SQLite）自动降级 LIKE。
FTS_DDL = ("CREATE VIRTUAL TABLE IF NOT EXISTS fts_item USING "
           "fts5(item_id UNINDEXED, name, notes, region, variety, era, denomination, "
           "cert_no, item_code, intro_text, catalog_ref, tokenize='trigram')")
FTS_FIELDS = ("name", "notes", "region", "variety", "era", "denomination",
              "cert_no", "item_code", "intro_text", "catalog_ref")
FTS_OK = False


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")   # 性能/耐久平衡，配合 WAL
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str,
                   column: str, ddl: str) -> None:
    """极简 schema 迁移：缺列就补。以后升级加字段都用这个，老库平滑升级。"""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    global FTS_OK
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # 平滑升级：老库补新列
        for col, ddl in (("send_express", "TEXT"), ("send_fee", "REAL"),
                         ("level", "TEXT"), ("catalog_ref", "TEXT"),
                         ("condition_st", "TEXT"), ("intro_text", "TEXT"),
                         ("intro_audio", "TEXT")):
            _ensure_column(conn, "item", col, ddl)
        _ensure_column(conn, "dict_entry", "code", "TEXT")     # 品类/时代编码（编号体系用）
        _ensure_column(conn, "item", "pub_views", "INTEGER DEFAULT 0")  # 导览浏览数
        _ensure_column(conn, "location", "grid_x", "INTEGER")   # 场馆平面图坐标
        _ensure_column(conn, "location", "grid_y", "INTEGER")
        _ensure_column(conn, "location", "grid_w", "INTEGER DEFAULT 1")  # 格子跨距
        _ensure_column(conn, "location", "grid_h", "INTEGER DEFAULT 1")
        try:
            # FTS 结构随版本演进（字段可能增减），启动时删除重建；
            # 索引数据本来就全量来自 item 表，紧随其后的 fts_rebuild 无损补齐
            conn.execute("DROP TABLE IF EXISTS fts_item")
            conn.execute(FTS_DDL)
            FTS_OK = True
        except Exception:
            FTS_OK = False
        fts_rebuild(conn)
        conn.execute("ANALYZE")   # 让查询计划器跟上数据分布


def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        r = conn.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
    return (r["value"] if r and r["value"] is not None else "") or default


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO setting(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))


def fts_rebuild(conn: sqlite3.Connection) -> None:
    """启动时同步全文索引（增量小、成本可忽略；防半路数据不一致）。"""
    if not FTS_OK:
        return
    rows = conn.execute(
        "SELECT id, name, notes, region, variety, era, denomination, cert_no, item_code, "
        "intro_text, catalog_ref FROM item").fetchall()
    conn.execute("DELETE FROM fts_item")
    for r in rows:
        fts_sync(conn, r["id"], dict(r))


def fts_sync(conn: sqlite3.Connection, item_id: int, values: dict) -> None:
    """藏品建档/修改后同步全文索引。"""
    if not FTS_OK:
        return
    conn.execute("DELETE FROM fts_item WHERE rowid=?", (item_id,))
    if any(values.get(f) for f in FTS_FIELDS):
        conn.execute(
            "INSERT INTO fts_item(rowid, item_id, name, notes, region, variety, era, "
            "denomination, cert_no, item_code, intro_text, catalog_ref) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, item_id, values.get("name"), values.get("notes"),
             values.get("region"), values.get("variety"), values.get("era"),
             values.get("denomination"), values.get("cert_no"),
             values.get("item_code"), values.get("intro_text"),
             values.get("catalog_ref")))


def fts_delete(conn: sqlite3.Connection, item_id: int) -> None:
    if FTS_OK:
        conn.execute("DELETE FROM fts_item WHERE rowid=?", (item_id,))


def fts_search_ids(conn: sqlite3.Connection, q: str,
                   limit: int = 800) -> Optional[list]:
    """中文全文检索。返回 id 列表（可能为空=确认无结果）；
    返回 None = 本次不走 FTS（库不支持或查询词不足 3 字），调用方降级 LIKE。"""
    q = (q or "").strip()
    if not FTS_OK or len(q) < 3:
        return None
    try:
        rows = conn.execute(
            "SELECT item_id FROM fts_item WHERE fts_item MATCH ? LIMIT ?",
            (f'"{q}"', limit)).fetchall()
        return [r["item_id"] for r in rows]
    except Exception:
        return None
