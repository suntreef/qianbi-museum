"""SQLite 建表与连接。单文件库，备份 = 复制 data/museum.db。"""
import sqlite3

from .config import DB_PATH

SCHEMA = """
-- 字典表：下拉选项与筛选维度的数据源，后台可增改，不用改代码
CREATE TABLE IF NOT EXISTS dict_entry (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    type    TEXT NOT NULL,   -- category/dynasty/bank/note_type/printer/grade_co/material
    name    TEXT NOT NULL,   -- 显示名，如 中央银行
    aliases TEXT,            -- 别名(逗号分隔)：英文行名、缩写，搜索与标签映射用
    sort    INTEGER DEFAULT 0,
    UNIQUE(type, name)
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
    grade_co        TEXT,                       -- 评级公司：PMG/公博/爱藏/华夏/闻德/保粹/PCGS/NGC/无
    cert_no         TEXT UNIQUE,                -- 评级编号，唯一键：防重+防假盒
    cert_url        TEXT,                       -- 证书页 URL，来源可追溯
    grade_raw       TEXT,                       -- 分数原文，如 MS 63 / 美品 78
    grade_scale     TEXT,                       -- 分数制：70制/85制/其他（不跨公司换算）
    serial_no       TEXT,                       -- 纸币冠字号
    status          TEXT NOT NULL DEFAULT 'draft',  -- draft/in_stock/reviewing/exhibited/lent/consigned/sold/lost
    location_id     INTEGER REFERENCES location(id),
    source          TEXT,                       -- 购入/捐赠/代管/交换
    purchase_price  REAL,
    purchase_date   TEXT,
    est_value       REAL,
    est_date        TEXT,
    notes           TEXT,
    created_by      TEXT,
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_item_category  ON item(category);
CREATE INDEX IF NOT EXISTS idx_item_bank      ON item(bank);
CREATE INDEX IF NOT EXISTS idx_item_note_type ON item(note_type);
CREATE INDEX IF NOT EXISTS idx_item_series    ON item(series);
CREATE INDEX IF NOT EXISTS idx_item_year      ON item(year_sort);
CREATE INDEX IF NOT EXISTS idx_item_status    ON item(status);

CREATE TABLE IF NOT EXISTS photo (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES item(id),
    shot_type   TEXT,                           -- front/back/edge/label/holder/detail
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
    adapter     TEXT,                           -- 内置适配器名 / semi_auto / generic_llm
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
    code      TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL REFERENCES item(id),
    kind       TEXT,                            -- 入库/送评/借出/归还/出售/估值变更/状态变更
    detail     TEXT,
    operator   TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_event_item ON event(item_id);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
