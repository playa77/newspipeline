"""SQLite persistence for pipeline data (stdlib sqlite3, no ORM).

Schema notes:
- recipient/interest scopes isolate deduplication and theme membership
- interests/feeds are NOT here — they live in config/settings.toml
  (DECISIONS.md D3); runs reference interests by name (D5)

Tables: interest_scopes, runs, articles, themes, theme_articles,
deliverables, briefs, pending_deliveries.
A single connection guarded by a lock is shared between the Flask thread
and the scheduler thread; WAL mode allows concurrent readers.
"""

# version: 0.6.0

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

_SCOPES_SCHEMA = """
CREATE TABLE IF NOT EXISTS interest_scopes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id TEXT NOT NULL DEFAULT '',
    interest_name TEXT NOT NULL,
    UNIQUE(recipient_id, interest_name),
    UNIQUE(id, recipient_id, interest_name)
);
"""

_SCHEMA = _SCOPES_SCHEMA + """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interest_name TEXT NOT NULL,
    recipient_id TEXT NOT NULL DEFAULT '',
    scope_id INTEGER NOT NULL REFERENCES interest_scopes(id),
    status TEXT NOT NULL DEFAULT 'running',   -- running | completed | failed
    current_stage TEXT,                        -- scrape | analyze | generate | brief
    error TEXT,
    run_date TEXT NOT NULL,                    -- YYYY-MM-DD (UTC)
    started_at TEXT NOT NULL,                  -- ISO 8601 UTC
    completed_at TEXT,
    UNIQUE(id, scope_id),
    FOREIGN KEY(scope_id, recipient_id, interest_name)
        REFERENCES interest_scopes(id, recipient_id, interest_name) ON UPDATE CASCADE
);
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    feed_name TEXT NOT NULL,
    scope_id INTEGER NOT NULL REFERENCES interest_scopes(id),
    url TEXT NOT NULL,                         -- dedupe within this interest scope
    title TEXT NOT NULL,
    author TEXT,
    published_at TEXT,
    scraped_at TEXT NOT NULL,
    excerpt TEXT,
    content TEXT,
    content_status TEXT NOT NULL,              -- full | excerpt | word_count
    UNIQUE(scope_id, url),
    UNIQUE(id, run_id),
    FOREIGN KEY(run_id, scope_id) REFERENCES runs(id, scope_id)
);
CREATE TABLE IF NOT EXISTS themes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    title TEXT NOT NULL,
    description TEXT,
    source_article_ids TEXT,                   -- JSON array of article numbers
    order_index INTEGER NOT NULL,
    UNIQUE(id, run_id)
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

_EXTRA_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS runs_scope_pair ON runs(id, scope_id);
CREATE UNIQUE INDEX IF NOT EXISTS themes_run_pair ON themes(id, run_id);
CREATE INDEX IF NOT EXISTS runs_interest_history ON runs(recipient_id, interest_name, id);
CREATE TABLE IF NOT EXISTS theme_articles (
    theme_id INTEGER NOT NULL,
    article_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    PRIMARY KEY(theme_id, article_id),
    FOREIGN KEY(theme_id, run_id) REFERENCES themes(id, run_id),
    FOREIGN KEY(article_id, run_id) REFERENCES articles(id, run_id)
);
CREATE TABLE IF NOT EXISTS pending_deliveries (
    run_id INTEGER PRIMARY KEY REFERENCES runs(id),
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'sending', 'completed', 'failed')),
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS deliveries_due ON pending_deliveries(status, due_at);
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
        try:
            self._migrate()
        except BaseException:
            self._conn.close()
            raise
        with self._lock:
            self._conn.executescript(_SCHEMA + _EXTRA_SCHEMA)
        logger.info("Store opened — db=%s", self._path)

    def _migrate(self) -> None:
        """Transactional, id-preserving upgrade of the old globally unique URL schema."""
        columns = {r[1] for r in self._conn.execute("PRAGMA table_info(runs)")}
        if not columns or "scope_id" in columns:
            return
        # No renaming of old parent tables: existing child FKs keep pointing
        # to runs/articles. FK checks are restored and verified before commit.
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(_SCOPES_SCHEMA)
            self._conn.execute("INSERT INTO interest_scopes(recipient_id, interest_name) "
                               "SELECT '', interest_name FROM runs GROUP BY interest_name")
            runs_sql = _SCHEMA[_SCHEMA.index("CREATE TABLE IF NOT EXISTS runs"): _SCHEMA.index("CREATE TABLE IF NOT EXISTS articles")]
            self._conn.execute(runs_sql.replace("IF NOT EXISTS runs", "runs_new"))
            self._conn.execute("INSERT INTO runs_new(id,interest_name,recipient_id,scope_id,status,current_stage,error,run_date,started_at,completed_at) "
                               "SELECT r.id,r.interest_name,'',s.id,r.status,r.current_stage,r.error,r.run_date,r.started_at,r.completed_at "
                               "FROM runs r JOIN interest_scopes s ON s.interest_name=r.interest_name AND s.recipient_id=''")
            self._conn.execute("DROP TABLE runs")
            self._conn.execute("ALTER TABLE runs_new RENAME TO runs")
            articles_sql = _SCHEMA[_SCHEMA.index("CREATE TABLE IF NOT EXISTS articles"): _SCHEMA.index("CREATE TABLE IF NOT EXISTS themes")]
            self._conn.execute(articles_sql.replace("IF NOT EXISTS articles", "articles_new"))
            self._conn.execute("INSERT INTO articles_new(id,run_id,scope_id,feed_name,url,title,author,published_at,scraped_at,excerpt,content,content_status) "
                               "SELECT a.id,a.run_id,r.scope_id,a.feed_name,a.url,a.title,a.author,a.published_at,a.scraped_at,a.excerpt,a.content,a.content_status "
                               "FROM articles a JOIN runs r ON r.id=a.run_id")
            if self._conn.execute("SELECT count(*) FROM articles_new").fetchone()[0] != self._conn.execute("SELECT count(*) FROM articles").fetchone()[0]:
                raise ValueError("database migration found invalid article foreign keys")
            self._conn.execute("DROP TABLE articles")
            self._conn.execute("ALTER TABLE articles_new RENAME TO articles")
            # Old themes have no composite uniqueness until this index is added.
            for statement in _EXTRA_SCHEMA.split(';'):
                if statement.strip():
                    self._conn.execute(statement)
            # Preserve and normalize valid old theme source indices into FK links.
            for theme in self._conn.execute("SELECT * FROM themes").fetchall():
                articles = self._conn.execute("SELECT id FROM articles WHERE run_id=? ORDER BY id", (theme["run_id"],)).fetchall()
                indices = json.loads(theme["source_article_ids"] or "[]")
                for index in indices:
                    if type(index) is int and 1 <= index <= len(articles):
                        self._conn.execute("INSERT OR IGNORE INTO theme_articles VALUES (?,?,?)",
                                           (theme["id"], articles[index-1]["id"], theme["run_id"]))
            if self._conn.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("database migration found invalid foreign keys")
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

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

    def create_run(self, interest_name: str, recipient_id: str = "") -> dict[str, Any]:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO interest_scopes(recipient_id, interest_name) VALUES (?,?) ON CONFLICT DO NOTHING", (recipient_id, interest_name))
            scope = self._conn.execute("SELECT id FROM interest_scopes WHERE recipient_id=? AND interest_name=?", (recipient_id, interest_name)).fetchone()[0]
            cur = self._conn.execute(
                "INSERT INTO runs (interest_name, recipient_id, scope_id, status, started_at, run_date) VALUES (?, ?, ?, 'running', ?, ?)",
                (interest_name, recipient_id, scope, utcnow_iso(), today_str()),
            )
            row = self._conn.execute("SELECT * FROM runs WHERE id=?", (cur.lastrowid,)).fetchone()
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

    def list_runs(self, interest_name: str, limit: int = 20, recipient_id: str = "") -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT * FROM runs WHERE interest_name = ? AND recipient_id = ? ORDER BY id DESC LIMIT ?",
            (interest_name, recipient_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def is_interest_running(self, interest_name: str, recipient_id: str = "") -> bool:
        row = self._execute(
            "SELECT 1 FROM runs WHERE interest_name = ? AND recipient_id = ? AND status = 'running' LIMIT 1",
            (interest_name, recipient_id),
        ).fetchone()
        return row is not None

    def rename_interest(self, old_name: str, new_name: str, recipient_id: str = "") -> int:
        return self.rename_interests(old_name, new_name, [recipient_id])

    def rename_interests(self, old_name: str, new_name: str, recipient_ids: list[str]) -> int:
        """Rename stable scopes atomically, including inheriting children.

        Database first, config second. A retry after the database commit is
        idempotent; existing destination histories are never merged.
        """
        count = 0
        with self._lock, self._conn:
            for recipient_id in recipient_ids:
                row = self._conn.execute("SELECT id FROM interest_scopes WHERE recipient_id=? AND interest_name=?", (recipient_id, old_name)).fetchone()
                if row is None:
                    continue
                busy = self._conn.execute("SELECT 1 FROM runs r LEFT JOIN pending_deliveries d ON d.run_id=r.id WHERE r.scope_id=? AND (r.status='running' OR d.status='sending') LIMIT 1", (row[0],)).fetchone()
                if busy:
                    raise ValueError("interest has an active run or delivery; retry after it finishes")
                count += self._conn.execute("SELECT count(*) FROM runs WHERE scope_id=?", (row[0],)).fetchone()[0]
                self._conn.execute("UPDATE interest_scopes SET interest_name=? WHERE id=?", (new_name, row[0]))
                for queued in self._conn.execute("SELECT d.run_id,d.payload FROM pending_deliveries d JOIN runs r ON r.id=d.run_id WHERE r.scope_id=? AND d.status IN ('pending','failed')", (row[0],)).fetchall():
                    payload = json.loads(queued["payload"])
                    payload["interest"]["name"] = new_name
                    self._conn.execute("UPDATE pending_deliveries SET payload=? WHERE run_id=?", (json.dumps(payload), queued["run_id"]))
        return count

    def sweep_orphaned_runs(self) -> int:
        """Mark every 'running' row as failed ('interrupted by restart').

        Called ONLY at serve startup (DECISIONS.md D10), before catch-up:
        a fresh serve start implies the previous process died. One-shot
        `run` invocations never call this — a concurrent serve process may
        legitimately own a running row. Returns the number of swept rows.
        """
        rows = self._execute(
            "SELECT id, interest_name FROM runs WHERE status = 'running'",
        ).fetchall()
        if not rows:
            return 0
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET status = 'failed', error = ?, completed_at = ? "
                "WHERE status = 'running'",
                ("interrupted by restart", utcnow_iso()),
            )
        logger.warning(
            "Startup sweep: %d orphaned 'running' run(s) marked failed — %s",
            len(rows),
            ", ".join(f"run {r['id']} ({r['interest_name']})" for r in rows),
        )
        return len(rows)

    # ---- Articles ----

    def article_exists(self, url: str, *, run_id: int) -> bool:
        """A URL seen previously in this run's recipient/interest scope."""
        return self._execute("SELECT 1 FROM articles a JOIN runs r ON r.scope_id=a.scope_id WHERE r.id=? AND a.url=? LIMIT 1", (run_id, url)).fetchone() is not None

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
               (run_id, scope_id, feed_name, url, title, author, published_at, scraped_at,
                excerpt, content, content_status)
               VALUES (?, (SELECT scope_id FROM runs WHERE id=?), ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (scope_id, url) DO NOTHING""",
            (run_id, run_id, feed_name, url, title, author, parse_rss_date(published_at),
             utcnow_iso(), excerpt, content, content_status),
        )
        return cur.rowcount > 0

    def get_articles_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._execute("SELECT * FROM articles WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- Themes ----

    def get_articles_by_ids(self, run_id: int, article_ids: list[int]) -> list[dict[str, Any]]:
        """Fetch articles by their (1-based) index within a run's article list.

        Out-of-range indices are silently dropped; order follows ``article_ids``.
        """
        if not article_ids:
            return []
        rows = self._execute(
            "SELECT * FROM articles WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        articles = [dict(r) for r in rows]
        result = []
        for aid in article_ids:
            if 1 <= aid <= len(articles):
                result.append(articles[aid - 1])
        return result

    def insert_theme(
        self, *, run_id: int, title: str, description: str,
        source_article_ids: list[int], order_index: int,
    ) -> dict[str, Any]:
        articles = self.get_articles_for_run(run_id)
        indices = list(dict.fromkeys(i for i in source_article_ids if type(i) is int and 1 <= i <= len(articles)))
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO themes (run_id, title, description, source_article_ids, order_index) VALUES (?, ?, ?, ?, ?)",
                (run_id, title, description, json.dumps(indices), order_index),
            )
            for index in indices:
                self._conn.execute("INSERT INTO theme_articles VALUES (?,?,?)", (cur.lastrowid, articles[index-1]["id"], run_id))
            row = self._conn.execute("SELECT * FROM themes WHERE id=?", (cur.lastrowid,)).fetchone()
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

    def insert_brief(self, *, run_id: int, content: str, word_count: int) -> None:
        """Store the brief document JSON with its prose word count.

        ``word_count`` is computed from the document (Document.word_count),
        not from the stored JSON — the JSON is not prose.
        """
        self._execute(
            "INSERT INTO briefs (run_id, content, word_count) VALUES (?, ?, ?)",
            (run_id, content, word_count),
        )

    def get_brief_for_run(self, run_id: int) -> dict[str, Any] | None:
        return self._row_to_dict(
            self._execute("SELECT * FROM briefs WHERE run_id = ? LIMIT 1", (run_id,)).fetchone()
        )

    # ---- Durable delivery queue (at-least-once after a crash) ----

    def queue_delivery(self, run_id: int, due_at: str, payload: dict) -> None:
        self._execute("INSERT INTO pending_deliveries(run_id,due_at,payload) VALUES (?,?,?) ON CONFLICT(run_id) DO NOTHING",
                      (run_id, due_at, json.dumps(payload)))

    def recover_deliveries(self) -> None:
        self._execute("UPDATE pending_deliveries SET status='pending' WHERE status='sending'")

    def claim_due_delivery(self, now: str) -> dict | None:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM pending_deliveries WHERE status='pending' AND due_at<=? ORDER BY due_at,run_id LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            cur = self._conn.execute("UPDATE pending_deliveries SET status='sending', attempts=attempts+1 WHERE run_id=? AND status='pending'", (row["run_id"],))
            return dict(row) if cur.rowcount else None

    def finish_delivery(self, run_id: int, error: str | None = None) -> None:
        self._execute("UPDATE pending_deliveries SET status=?,error=?,delivered_at=? WHERE run_id=?",
                      ("failed" if error else "completed", error, None if error else utcnow_iso(), run_id))

    def list_deliveries(self, recipient_id: str = "", interest_name: str | None = None) -> list[dict]:
        sql = "SELECT d.run_id,d.due_at,d.status,d.attempts,d.error,d.delivered_at FROM pending_deliveries d JOIN runs r ON r.id=d.run_id WHERE r.recipient_id=?"
        params = (recipient_id,)
        if interest_name is not None:
            sql += " AND r.interest_name=?"
            params += (interest_name,)
        return [dict(r) for r in self._execute(sql + " ORDER BY d.run_id DESC", params).fetchall()]

    def retry_delivery(self, run_id: int, recipient_id: str) -> bool:
        return self._execute("UPDATE pending_deliveries SET status='pending',error=NULL WHERE run_id=? AND status='failed' AND run_id IN (SELECT id FROM runs WHERE recipient_id=?)", (run_id, recipient_id)).rowcount > 0
