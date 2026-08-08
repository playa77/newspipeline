"""SQLite persistence for pipeline data (stdlib sqlite3, no ORM).

Schema notes:
- single-user local app, no user scoping
- interests/feeds are NOT here — they live in config/settings.toml
  (DECISIONS.md D3); runs reference interests by name (D5)

Tables: runs, articles, themes, deliverables, briefs.
A single connection guarded by a lock is shared between the Flask thread
and the scheduler thread; WAL mode allows concurrent readers.
"""

# version: 0.1.0

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from newspipeline.config import DB_PATH, DATA_DIR

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interest_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',   -- running | completed | failed
    current_stage TEXT,                        -- scrape | analyze | generate | brief
    error TEXT,
    run_date TEXT NOT NULL,                    -- YYYY-MM-DD (UTC)
    started_at TEXT NOT NULL,                  -- ISO 8601 UTC
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    feed_name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,                  -- dedupe across all runs (G2)
    title TEXT NOT NULL,
    author TEXT,
    published_at TEXT,
    scraped_at TEXT NOT NULL,
    excerpt TEXT,
    content TEXT,
    content_status TEXT NOT NULL               -- full | excerpt | word_count
);
CREATE TABLE IF NOT EXISTS themes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    title TEXT NOT NULL,
    description TEXT,
    source_article_ids TEXT,                   -- JSON array of article numbers
    order_index INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS deliverables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id INTEGER NOT NULL REFERENCES themes(id),
    deliverable_type TEXT NOT NULL,            -- summary | script | script_de
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    content TEXT NOT NULL,
    word_count INTEGER NOT NULL
);
"""

_ALLOWED_RUN_UPDATE_COLS = {"status", "completed_at", "current_stage", "error"}


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def parse_rss_date(raw: str) -> str:
    """Parse an RSS pubDate into an ISO 8601 UTC string; fall back to now.

    RFC 2822 first, then common ISO variants; naive values assumed UTC.
    """
    if raw and raw.strip():
        raw = raw.strip()
        try:
            return parsedate_to_datetime(raw).astimezone(UTC).isoformat(timespec="seconds")
        except (ValueError, TypeError):
            pass
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(raw, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt.astimezone(UTC).isoformat(timespec="seconds")
            except ValueError:
                continue
    logger.info("Unparseable RSS date %r — falling back to now", raw[:80] if raw else raw)
    return utcnow_iso()


class Store:
    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        self._path = Path(db_path)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
        logger.info("Store opened — db=%s", self._path)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    # ---- Runs ----

    def create_run(self, interest_name: str) -> dict[str, Any]:
        cur = self._execute(
            "INSERT INTO runs (interest_name, status, started_at, run_date) VALUES (?, 'running', ?, ?)",
            (interest_name, utcnow_iso(), today_str()),
        )
        row = self._execute("SELECT * FROM runs WHERE id = ?", (cur.lastrowid,)).fetchone()
        logger.info("Run %d created — interest=%r", cur.lastrowid, interest_name)
        return dict(row)

    def update_run(self, run_id: int, **fields: Any) -> None:
        disallowed = set(fields) - _ALLOWED_RUN_UPDATE_COLS
        if disallowed:
            raise ValueError(f"update_run: unknown columns {disallowed}")
        if not fields:
            return
        clauses = ", ".join(f"{k} = ?" for k in fields)
        self._execute(f"UPDATE runs SET {clauses} WHERE id = ?", (*fields.values(), run_id))

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        return self._row_to_dict(self._execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())

    def list_runs(self, interest_name: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT * FROM runs WHERE interest_name = ? ORDER BY id DESC LIMIT ?",
            (interest_name, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def is_interest_running(self, interest_name: str) -> bool:
        row = self._execute(
            "SELECT 1 FROM runs WHERE interest_name = ? AND status = 'running' LIMIT 1",
            (interest_name,),
        ).fetchone()
        return row is not None

    # ---- Articles ----

    def article_exists(self, url: str) -> bool:
        return self._execute("SELECT 1 FROM articles WHERE url = ? LIMIT 1", (url,)).fetchone() is not None

    def insert_article(
        self,
        *,
        run_id: int,
        feed_name: str,
        url: str,
        title: str,
        author: str | None,
        published_at: str,
        excerpt: str | None,
        content: str | None,
        content_status: str,
    ) -> bool:
        """Insert an article; skip silently on duplicate URL. Returns True if inserted."""
        cur = self._execute(
            """INSERT INTO articles
               (run_id, feed_name, url, title, author, published_at, scraped_at,
                excerpt, content, content_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (url) DO NOTHING""",
            (run_id, feed_name, url, title, author, parse_rss_date(published_at),
             utcnow_iso(), excerpt, content, content_status),
        )
        return cur.rowcount > 0

    def get_articles_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._execute("SELECT * FROM articles WHERE run_id = ?", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- Themes ----

    def insert_theme(
        self, *, run_id: int, title: str, description: str,
        source_article_ids: list[int], order_index: int,
    ) -> dict[str, Any]:
        cur = self._execute(
            "INSERT INTO themes (run_id, title, description, source_article_ids, order_index) VALUES (?, ?, ?, ?, ?)",
            (run_id, title, description, json.dumps(source_article_ids), order_index),
        )
        row = self._execute("SELECT * FROM themes WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def get_themes_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT * FROM themes WHERE run_id = ? ORDER BY order_index", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- Deliverables ----

    def insert_deliverable(self, *, theme_id: int, deliverable_type: str, content: str) -> None:
        self._execute(
            "INSERT INTO deliverables (theme_id, deliverable_type, content) VALUES (?, ?, ?)",
            (theme_id, deliverable_type, content),
        )

    def get_deliverables_for_theme(self, theme_id: int) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT * FROM deliverables WHERE theme_id = ? ORDER BY deliverable_type", (theme_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- Briefs ----

    def insert_brief(self, *, run_id: int, content: str) -> None:
        self._execute(
            "INSERT INTO briefs (run_id, content, word_count) VALUES (?, ?, ?)",
            (run_id, content, len(content.split())),
        )

    def get_brief_for_run(self, run_id: int) -> dict[str, Any] | None:
        return self._row_to_dict(
            self._execute("SELECT * FROM briefs WHERE run_id = ? LIMIT 1", (run_id,)).fetchone()
        )
