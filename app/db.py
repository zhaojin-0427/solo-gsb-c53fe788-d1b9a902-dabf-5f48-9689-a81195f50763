"""SQLite 连接与表结构。

每次成功导入生成新的 dataset_id；查询总是读取 active 版本。
导入失败时事务回滚，旧数据继续可查。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "gtfs.db"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 数据集版本：每次导入一行，active 指向当前启用版本
CREATE TABLE IF NOT EXISTS datasets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    status     TEXT NOT NULL DEFAULT 'ready'
);

CREATE TABLE IF NOT EXISTS agency (
    dataset_id      INTEGER NOT NULL,
    agency_id       TEXT NOT NULL DEFAULT '',
    agency_name     TEXT NOT NULL,
    agency_timezone TEXT NOT NULL,
    PRIMARY KEY (dataset_id, agency_id)
);

CREATE TABLE IF NOT EXISTS routes (
    dataset_id   INTEGER NOT NULL,
    route_id     TEXT NOT NULL,
    agency_id    TEXT NOT NULL DEFAULT '',
    route_short_name TEXT,
    route_long_name  TEXT,
    route_type   INTEGER,
    PRIMARY KEY (dataset_id, route_id)
);

CREATE TABLE IF NOT EXISTS stops (
    dataset_id  INTEGER NOT NULL,
    stop_id     TEXT NOT NULL,
    stop_name   TEXT NOT NULL,
    stop_lat    REAL,
    stop_lon    REAL,
    location_type INTEGER DEFAULT 0,
    PRIMARY KEY (dataset_id, stop_id)
);

CREATE TABLE IF NOT EXISTS services (
    dataset_id INTEGER NOT NULL,
    service_id TEXT NOT NULL,
    -- 周历位掩码：bit0=周一 ... bit6=周日；NULL 表示仅由例外日期定义
    weekdays   INTEGER,
    start_date TEXT,
    end_date   TEXT,
    PRIMARY KEY (dataset_id, service_id)
);

CREATE TABLE IF NOT EXISTS service_exceptions (
    dataset_id   INTEGER NOT NULL,
    service_id   TEXT NOT NULL,
    date         TEXT NOT NULL,   -- YYYYMMDD
    exception_type INTEGER NOT NULL, -- 1=加开 2=停运
    PRIMARY KEY (dataset_id, service_id, date)
);

CREATE TABLE IF NOT EXISTS trips (
    dataset_id        INTEGER NOT NULL,
    trip_id           TEXT NOT NULL,
    route_id          TEXT NOT NULL,
    service_id        TEXT NOT NULL,
    direction_id      INTEGER,
    trip_headsign     TEXT,
    PRIMARY KEY (dataset_id, trip_id)
);

-- stop_times 中的时间统一存为"自服务日午夜起的秒数"，允许 >= 86400
CREATE TABLE IF NOT EXISTS stop_times (
    dataset_id     INTEGER NOT NULL,
    trip_id        TEXT NOT NULL,
    stop_sequence  INTEGER NOT NULL,
    stop_id        TEXT NOT NULL,
    arrival_time   INTEGER NOT NULL,
    departure_time INTEGER NOT NULL,
    pickup_type    INTEGER NOT NULL DEFAULT 0,
    dropoff_type   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transfers (
    dataset_id       INTEGER NOT NULL,
    from_stop_id     TEXT NOT NULL,
    to_stop_id       TEXT NOT NULL,
    transfer_type    INTEGER NOT NULL,
    min_transfer_time INTEGER
);

-- 查询热点索引（10 万级 stop_times 下 2 秒内返回的关键）
CREATE INDEX IF NOT EXISTS idx_st_active        ON services(dataset_id);
CREATE INDEX IF NOT EXISTS idx_exc_lookup       ON service_exceptions(dataset_id, date, service_id);
CREATE INDEX IF NOT EXISTS idx_trips_service    ON trips(dataset_id, service_id);
CREATE INDEX IF NOT EXISTS idx_st_abs_dep       ON stop_times(dataset_id, departure_time);
CREATE INDEX IF NOT EXISTS idx_st_trip          ON stop_times(dataset_id, trip_id, stop_sequence);
CREATE INDEX IF NOT EXISTS idx_st_stop          ON stop_times(dataset_id, stop_id, departure_time);
CREATE INDEX IF NOT EXISTS idx_trips_svc_route  ON trips(dataset_id, service_id, trip_id);
CREATE INDEX IF NOT EXISTS idx_transfers_from   ON transfers(dataset_id, from_stop_id);
CREATE INDEX IF NOT EXISTS idx_stops_name       ON stops(dataset_id, stop_name);
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_active_dataset(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT value FROM meta WHERE key='active_dataset'").fetchone()
    return int(row["value"]) if row else None
