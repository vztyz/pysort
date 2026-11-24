"""SQLite storage for pysort."""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY,
    path         TEXT UNIQUE NOT NULL,
    name         TEXT NOT NULL,
    ext          TEXT,
    size         INTEGER,
    mtime        INTEGER,
    drive        TEXT,
    category     TEXT,
    confidence   REAL,
    reasons      TEXT,
    project_root TEXT,
    ai_label     TEXT,
    ai_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_cat  ON files(category);
CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);

CREATE TABLE IF NOT EXISTS dirs (
    path     TEXT PRIMARY KEY,
    depth    INTEGER,
    size     INTEGER,      -- subtree size (all files)
    own_size INTEGER,      -- files directly in this dir
    files    INTEGER,      -- subtree file count
    py_files INTEGER,
    py_size  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_dirs_size ON dirs(size);

CREATE TABLE IF NOT EXISTS markers (
    path TEXT PRIMARY KEY,
    kind TEXT
);

CREATE TABLE IF NOT EXISTS ai_cache (
    key        TEXT PRIMARY KEY,
    label      TEXT,
    reason     TEXT,
    confidence REAL,
    model      TEXT,
    ts         INTEGER
);

CREATE TABLE IF NOT EXISTS hashes (
    path  TEXT PRIMARY KEY,
    size  INTEGER,
    mtime INTEGER,
    sha1  TEXT
);
"""


def connect(path):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-200000")
    conn.executescript(SCHEMA)
    return conn


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default
