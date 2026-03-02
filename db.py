"""
Database module -- shared SQLite connection and schema management.

Both app.py (Flask web server) and scraper.py (background worker) import
from here so the schema definition lives in exactly one place.

The database uses WAL journal mode for safe concurrent reads/writes
across the two processes and a 5-second busy timeout to handle brief
lock contention.
"""

import sqlite3
from contextlib import contextmanager

# ---------------------------------------------------------------------------
# Path to the SQLite database file (co-located with the application).
# ---------------------------------------------------------------------------
DB_PATH = "documents.db"

# ---------------------------------------------------------------------------
# Schema: every CREATE TABLE statement, kept in a single list so init_db()
# can iterate over them.  The order matters -- foreign keys reference
# earlier tables.
# ---------------------------------------------------------------------------
_SCHEMA = [
    # A website URL that should be periodically scraped.
    """
    CREATE TABLE IF NOT EXISTS scrape_sources (
        id              INTEGER PRIMARY KEY,
        workspace       TEXT NOT NULL,
        url             TEXT NOT NULL,
        category        TEXT NOT NULL,
        max_depth       INTEGER NOT NULL DEFAULT 1,
        crawl_mode      TEXT NOT NULL DEFAULT 'depth',
        allowed_prefixes TEXT,
        max_pages       INTEGER NOT NULL DEFAULT 100,
        schedule        TEXT,
        enabled         INTEGER NOT NULL DEFAULT 1,
        allow_offsite   INTEGER NOT NULL DEFAULT 0,
        offsite_depth   INTEGER NOT NULL DEFAULT 1,
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_scraped_at TIMESTAMP,
        UNIQUE(workspace, url)
    )
    """,
    # One row per crawl execution (pending -> running -> completed/failed).
    """
    CREATE TABLE IF NOT EXISTS scrape_jobs (
        id            INTEGER PRIMARY KEY,
        source_id     INTEGER NOT NULL,
        status        TEXT NOT NULL DEFAULT 'pending',
        requested_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        started_at    TIMESTAMP,
        completed_at  TIMESTAMP,
        pages_found   INTEGER DEFAULT 0,
        pages_scraped INTEGER DEFAULT 0,
        error         TEXT,
        FOREIGN KEY (source_id) REFERENCES scrape_sources(id)
    )
    """,
    # Unified document table -- stores uploads, scraped pages, and API-added
    # documents in a single place.  The ``source_type`` column discriminates
    # between origins ('upload', 'scrape', 'api').
    """
    CREATE TABLE IF NOT EXISTS documents (
        id              INTEGER PRIMARY KEY,
        workspace       TEXT NOT NULL,
        filename        TEXT NOT NULL,
        location        TEXT NOT NULL,
        source_type     TEXT NOT NULL DEFAULT 'upload',
        source_id       INTEGER,
        source_url      TEXT,
        title           TEXT,
        category        TEXT,
        depth           INTEGER DEFAULT 0,
        converted       INTEGER DEFAULT 0,
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(workspace, filename),
        FOREIGN KEY (source_id) REFERENCES scrape_sources(id)
    )
    """,
    # Index for efficient source_url uniqueness checks during scraping.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_source_url
    ON documents (workspace, source_url)
    WHERE source_url IS NOT NULL
    """,
    # Index for efficient filtering by source_type.
    """
    CREATE INDEX IF NOT EXISTS idx_documents_source_type
    ON documents (workspace, source_type)
    """,
    # Index for efficient lookups of documents belonging to a scrape source.
    """
    CREATE INDEX IF NOT EXISTS idx_documents_source_id
    ON documents (source_id)
    WHERE source_id IS NOT NULL
    """,
]


def get_db():
    """Open a new SQLite connection with WAL mode and Row factory.

    Callers should use the ``open_db()`` context manager instead of calling
    this directly, unless they need manual connection lifetime control.
    """
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db


@contextmanager
def open_db():
    """Context manager that yields a database connection and guarantees close.

    Usage::

        with open_db() as db:
            db.execute("SELECT ...")
    """
    db = get_db()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables and indexes if they don't already exist."""
    with open_db() as db:
        for ddl in _SCHEMA:
            try:
                db.execute(ddl)
            except sqlite3.OperationalError:
                # Safe to ignore -- table/index already exists.
                pass

        # Migrations: add columns that may be missing in existing databases.
        _migrations = [
            "ALTER TABLE scrape_sources ADD COLUMN allow_offsite INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE scrape_sources ADD COLUMN offsite_depth INTEGER NOT NULL DEFAULT 1",
        ]
        for stmt in _migrations:
            try:
                db.execute(stmt)
            except sqlite3.OperationalError:
                # Column already exists -- safe to ignore.
                pass

        db.commit()
