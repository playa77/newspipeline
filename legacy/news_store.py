"""News Plugin — data store (PostgreSQL-backed).

Replaces the SQLite-based Database class from ai_news_scraper.
Tables: news_interests, news_feeds, news_runs, news_articles, news_themes, news_deliverables, news_briefs
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_ALLOWED_RUN_UPDATE_COLS = {"status", "completed_at", "current_stage", "error"}


class NewsStore:
    def __init__(self, session: AsyncSession):
        self._s = session

    def _utcnow_dt(self) -> datetime:
        """Return a naive datetime for TIMESTAMP WITHOUT TIME ZONE columns."""
        return datetime.now(UTC).replace(tzinfo=None)

    def _todays_date_str(self) -> str:
        return datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d")

    @staticmethod
    def _parse_date(raw: str) -> datetime | None:
        """Parse an RSS pubDate string into a naive UTC datetime.

        Tries email.utils.parsedate_to_datetime first (RFC 2822),
        then a handful of common ISO-ish variants.  Returns None on failure.
        """
        if not raw or not raw.strip():
            return None
        raw = raw.strip()
        # -- RFC 2822 (the most common RSS format) --
        try:
            dt = parsedate_to_datetime(raw)
            return dt.replace(tzinfo=None)
        except (ValueError, TypeError):
            pass
        # -- ISO 8601 / common fallbacks --
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(raw, fmt)
                if dt.tzinfo is not None:
                    dt = dt.astimezone(UTC).replace(tzinfo=None)
                return dt
            except ValueError:
                continue
        return None

    # ---- Interests ----
    async def list_interests(self, user_id: str) -> list[dict[str, Any]]:
        rows = await self._s.execute(
            text(
                "SELECT ni.*, COALESCE(fc.cnt, 0) AS feed_count "
                "FROM news_interests ni "
                "LEFT JOIN (SELECT interest_id, COUNT(*) AS cnt FROM news_feeds GROUP BY interest_id) fc "
                "ON fc.interest_id = ni.id "
                "WHERE ni.user_id = :uid ORDER BY ni.name"
            ),
            {"uid": user_id},
        )
        return [dict(r._mapping) for r in rows]

    async def get_interest(self, user_id: str, interest_id: int) -> dict[str, Any] | None:
        row = await self._s.execute(
            text("SELECT * FROM news_interests WHERE id = :iid AND user_id = :uid"),
            {"iid": interest_id, "uid": user_id},
        )
        r = row.first()
        if r is None:
            return None
        return dict(r._mapping)

    async def create_interest(self, user_id: str, data: dict[str, Any]) -> dict[str, Any]:
        result = await self._s.execute(
            text(
                """INSERT INTO news_interests
                   (user_id, name, start_time, interval_hours, target_summary_words,
                    target_script_words, target_script_de_words, target_brief_words,
                    enable_summary, enable_script, enable_script_de,
                    enable_brief, enable_email, input_data_length_mode, input_word_count,
                    email_sender, email_recipient)
                   VALUES (:uid, :name, :start_time, :interval_hours,
                           :target_summary_words, :target_script_words,
                           :target_script_de_words, :target_brief_words,
                           :enable_summary, :enable_script, :enable_script_de,
                           :enable_brief, :enable_email,
                           :input_data_length_mode, :input_word_count,
                           :email_sender, :email_recipient)
                   RETURNING *"""
            ),
            {
                "uid": user_id,
                "name": data.get("name", "New Interest"),
                "start_time": data.get("start_time", "04:00"),
                "interval_hours": data.get("interval_hours", 24),
                "target_summary_words": data.get("target_summary_words", 750),
                "target_script_words": data.get("target_script_words", 1250),
                "target_script_de_words": data.get("target_script_de_words", 1250),
                "target_brief_words": data.get("target_brief_words", 600),
                "enable_summary": data.get("enable_summary", True),
                "enable_script": data.get("enable_script", True),
                "enable_script_de": data.get("enable_script_de", False),
                "enable_brief": data.get("enable_brief", True),
                "enable_email": data.get("enable_email", False),
                "input_data_length_mode": data.get("input_data_length_mode", "full_article"),
                "input_word_count": data.get("input_word_count", 256),
                "email_sender": data.get("email_sender", ""),
                "email_recipient": data.get("email_recipient", ""),
            },
        )
        await self._s.commit()
        r = result.first()
        if r is None:
            raise RuntimeError("Failed to create interest")
        return dict(r._mapping)

    async def delete_interest(self, user_id: str, interest_id: int) -> None:
        await self._s.execute(
            text("DELETE FROM news_interests WHERE id = :iid AND user_id = :uid"),
            {"iid": interest_id, "uid": user_id},
        )
        await self._s.commit()

    # ---- Feeds ----
    async def list_feeds(self, user_id: str, interest_id: int) -> list[dict[str, Any]]:
        rows = await self._s.execute(
            text(
                "SELECT nf.* FROM news_feeds nf "
                "JOIN news_interests ni ON ni.id = nf.interest_id "
                "WHERE nf.interest_id = :iid AND ni.user_id = :uid "
                "ORDER BY nf.name"
            ),
            {"iid": interest_id, "uid": user_id},
        )
        return [dict(r._mapping) for r in rows]

    async def add_feed(
        self, user_id: str, interest_id: int, url: str, name: str, category: str
    ) -> dict[str, Any]:
        interest = await self.get_interest(user_id, interest_id)
        if interest is None:
            raise ValueError("Interest not found or access denied")

        result = await self._s.execute(
            text(
                """INSERT INTO news_feeds (interest_id, url, name, category)
                   VALUES (:iid, :url, :name, :cat)
                   ON CONFLICT (interest_id, url) DO UPDATE
                      SET name = :name2, category = :cat2
                   RETURNING *"""
            ),
            {"iid": interest_id, "url": url, "name": name, "cat": category, "name2": name, "cat2": category},
        )
        await self._s.commit()
        r = result.first()
        if r is None:
            raise RuntimeError("Failed to add feed")
        return dict(r._mapping)

    async def delete_feed(self, user_id: str, interest_id: int, feed_id: int) -> None:
        await self._s.execute(
            text(
                "DELETE FROM news_feeds "
                "WHERE id = :fid AND interest_id = :iid "
                "AND interest_id IN (SELECT id FROM news_interests WHERE user_id = :uid)"
            ),
            {"fid": feed_id, "iid": interest_id, "uid": user_id},
        )
        await self._s.commit()

    async def get_feeds_for_interest(self, interest_id: int) -> list[dict[str, Any]]:
        rows = await self._s.execute(
            text("SELECT * FROM news_feeds WHERE interest_id = :iid"),
            {"iid": interest_id},
        )
        return [dict(r._mapping) for r in rows]

    # ---- Pipeline Runs ----
    async def create_run(self, interest_id: int) -> dict[str, Any]:
        result = await self._s.execute(
            text(
                """INSERT INTO news_runs (interest_id, status, started_at, run_date)
                   VALUES (:iid, 'running', :now, :today)
                   RETURNING *"""
            ),
            {"iid": interest_id, "now": self._utcnow_dt(), "today": self._todays_date_str()},
        )
        await self._s.commit()
        r = result.first()
        if r is None:
            raise RuntimeError("Failed to create run")
        return dict(r._mapping)

    async def update_run(self, run_id: int, **kwargs: Any) -> None:
        disallowed = set(kwargs) - _ALLOWED_RUN_UPDATE_COLS
        if disallowed:
            raise ValueError(f"update_run: unknown columns {disallowed}")
        if not kwargs:
            return
        set_clauses = ", ".join(f"{k} = :{k}" for k in kwargs)
        await self._s.execute(
            text(f"UPDATE news_runs SET {set_clauses} WHERE id = :rid"),
            {**kwargs, "rid": run_id},
        )
        await self._s.commit()

    async def list_runs(self, user_id: str, interest_id: int) -> list[dict[str, Any]]:
        rows = await self._s.execute(
            text(
                "SELECT nr.* FROM news_runs nr "
                "JOIN news_interests ni ON ni.id = nr.interest_id "
                "WHERE nr.interest_id = :iid AND ni.user_id = :uid "
                "ORDER BY nr.id DESC LIMIT 20"
            ),
            {"iid": interest_id, "uid": user_id},
        )
        return [dict(r._mapping) for r in rows]

    async def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = await self._s.execute(text("SELECT * FROM news_runs WHERE id = :rid"), {"rid": run_id})
        r = row.first()
        if r is None:
            return None
        return dict(r._mapping)

    async def get_run_for_user(self, user_id: str, run_id: int) -> dict[str, Any] | None:
        row = await self._s.execute(
            text(
                "SELECT nr.* FROM news_runs nr "
                "JOIN news_interests ni ON ni.id = nr.interest_id "
                "WHERE nr.id = :rid AND ni.user_id = :uid"
            ),
            {"rid": run_id, "uid": user_id},
        )
        r = row.first()
        if r is None:
            return None
        return dict(r._mapping)

    # ---- Articles ----
    async def article_exists(self, url: str) -> bool:
        row = await self._s.execute(text("SELECT 1 FROM news_articles WHERE url = :url LIMIT 1"), {"url": url})
        return row.first() is not None

    async def insert_article(
        self,
        run_id: int,
        feed_id: int,
        url: str,
        title: str,
        author: str | None,
        published_at: str,
        excerpt: str | None,
        content: str | None,
        content_status: str,
    ) -> dict[str, Any]:
        # Parse published_at from RSS string to datetime.
        # RSS feeds emit wildly different formats; fall back to now on failure.
        parsed_published = self._parse_date(published_at) or self._utcnow_dt()
        result = await self._s.execute(
            text(
                """INSERT INTO news_articles
                   (run_id, feed_id, url, title, author, published_at,
                    scraped_at, excerpt, content, content_status)
                   VALUES (:rid, :fid, :url, :title, :author, :pat,
                           :now, :excerpt, :content, :cs)
                   ON CONFLICT (url) DO NOTHING
                   RETURNING *"""
            ),
            {
                "rid": run_id, "fid": feed_id, "url": url, "title": title,
                "author": author, "pat": parsed_published, "now": self._utcnow_dt(),
                "excerpt": excerpt, "content": content, "cs": content_status,
            },
        )
        await self._s.commit()
        r = result.first()
        return dict(r._mapping) if r else {}

    async def get_articles_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = await self._s.execute(text("SELECT * FROM news_articles WHERE run_id = :rid"), {"rid": run_id})
        return [dict(r._mapping) for r in rows]

    # ---- Themes ----
    async def insert_theme(
        self, run_id: int, title: str, description: str,
        source_article_ids: list[int], order_index: int,
    ) -> dict[str, Any]:
        result = await self._s.execute(
            text(
                """INSERT INTO news_themes (run_id, title, description, source_article_ids, order_index)
                   VALUES (:rid, :title, :desc, :saids, :oi)
                   RETURNING *"""
            ),
            {"rid": run_id, "title": title, "desc": description,
             "saids": json.dumps(source_article_ids), "oi": order_index},
        )
        await self._s.commit()
        r = result.first()
        if r is None:
            raise RuntimeError("Failed to insert theme")
        return dict(r._mapping)

    async def get_themes_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = await self._s.execute(
            text("SELECT * FROM news_themes WHERE run_id = :rid ORDER BY order_index"),
            {"rid": run_id},
        )
        return [dict(r._mapping) for r in rows]

    # ---- Deliverables ----
    async def insert_deliverable(self, theme_id: int, d_type: str, content: str) -> dict[str, Any]:
        result = await self._s.execute(
            text(
                """INSERT INTO news_deliverables (theme_id, deliverable_type, content)
                   VALUES (:tid, :dt, :content)
                   RETURNING *"""
            ),
            {"tid": theme_id, "dt": d_type, "content": content},
        )
        await self._s.commit()
        r = result.first()
        if r is None:
            raise RuntimeError("Failed to insert deliverable")
        return dict(r._mapping)

    # ---- Briefs ----
    async def insert_brief(self, run_id: int, content: str) -> dict[str, Any]:
        result = await self._s.execute(
            text(
                """INSERT INTO news_briefs (run_id, content, word_count)
                   VALUES (:rid, :content, :wc)
                   RETURNING *"""
            ),
            {"rid": run_id, "content": content, "wc": len(content.split())},
        )
        await self._s.commit()
        r = result.first()
        if r is None:
            raise RuntimeError("Failed to insert brief")
        return dict(r._mapping)

    async def get_daily_brief_for_run(self, run_id: int) -> dict[str, Any] | None:
        row = await self._s.execute(text("SELECT * FROM news_briefs WHERE run_id = :rid LIMIT 1"), {"rid": run_id})
        r = row.first()
        if r is None:
            return None
        return dict(r._mapping)

    async def get_previous_brief(self, before_date: str) -> dict[str, Any] | None:
        row = await self._s.execute(
            text(
                "SELECT nb.* FROM news_briefs nb "
                "JOIN news_runs nr ON nb.run_id = nr.id "
                "WHERE nr.run_date < :bd AND nr.status = 'completed' "
                "ORDER BY nr.run_date DESC LIMIT 1"
            ),
            {"bd": before_date},
        )
        r = row.first()
        if r is None:
            return None
        return dict(r._mapping)

    # ---- Deliverable helpers ----
    async def get_deliverables_for_theme(self, theme_id: int) -> list[dict[str, Any]]:
        rows = await self._s.execute(
            text(
                "SELECT * FROM news_deliverables "
                "WHERE theme_id = :tid ORDER BY deliverable_type"
            ),
            {"tid": theme_id},
        )
        return [dict(r._mapping) for r in rows]

    async def update_interest(self, user_id: str, interest_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
        interest = await self.get_interest(user_id, interest_id)
        if interest is None:
            return None

        updatable = {
            "name", "start_time", "interval_hours",
            "target_summary_words", "target_script_words",
            "target_script_de_words", "target_brief_words",
            "enable_summary", "enable_script", "enable_script_de",
            "enable_brief", "enable_email",
            "input_data_length_mode", "input_word_count",
            "email_sender", "email_recipient",
        }
        updates = {k: v for k, v in data.items() if k in updatable and v is not None}
        if not updates:
            return interest

        set_clauses = ", ".join(f"{k} = :{k}" for k in updates)
        updates["iid"] = interest_id
        updates["uid"] = user_id
        await self._s.execute(
            text(
                f"UPDATE news_interests SET {set_clauses} "
                f"WHERE id = :iid AND user_id = :uid"
            ),
            updates,
        )
        await self._s.commit()
        return await self.get_interest(user_id, interest_id)

    async def update_feed(self, user_id: str, interest_id: int, feed_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
        interest = await self.get_interest(user_id, interest_id)
        if not interest:
            return None

        updatable = {"url", "name", "category"}
        updates = {k: v for k, v in data.items() if k in updatable and v is not None}
        if not updates:
            rows = await self._s.execute(
                text(
                    "SELECT * FROM news_feeds WHERE id = :fid AND interest_id = :iid"
                ),
                {"fid": feed_id, "iid": interest_id},
            )
            r = rows.first()
            return dict(r._mapping) if r else None

        set_clauses = ", ".join(f"{k} = :{k}" for k in updates)
        updates["fid"] = feed_id
        updates["iid"] = interest_id
        result = await self._s.execute(
            text(
                f"UPDATE news_feeds SET {set_clauses} "
                f"WHERE id = :fid AND interest_id = :iid "
                f"RETURNING *"
            ),
            updates,
        )
        await self._s.commit()
        r = result.first()
        return dict(r._mapping) if r else None

    async def is_interest_running(self, interest_id: int) -> bool:
        """Check if there's an active (running) pipeline for an interest."""
        row = await self._s.execute(
            text(
                "SELECT 1 FROM news_runs "
                "WHERE interest_id = :iid AND status = 'running' LIMIT 1"
            ),
            {"iid": interest_id},
        )
        return row.first() is not None

    async def list_all_interests_global(self) -> list[dict[str, Any]]:
        """List all interests across all users (for scheduler)."""
        rows = await self._s.execute(
            text("SELECT * FROM news_interests ORDER BY name")
        )
        return [dict(r._mapping) for r in rows]

    async def get_interest_by_id_global(self, interest_id: int) -> dict[str, Any] | None:
        """Fetch any interest by ID without user scope — for public endpoints like email verification."""
        row = await self._s.execute(
            text("SELECT * FROM news_interests WHERE id = :iid"),
            {"iid": interest_id},
        )
        r = row.first()
        if r is None:
            return None
        return dict(r._mapping)

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _generate_verification_token(self) -> tuple[str, str]:
        """Generate a raw token and its SHA-256 hash. Returns (raw, hash)."""
        raw = secrets.token_urlsafe(32)
        return raw, self._hash_token(raw)

    async def set_email_recipient_verification_token(self, interest_id: int) -> str:
        """Generate and store a verification token for an interest's email recipient.

        Returns the raw token (to embed in the verification link).
        The token hash and expiry (24h) are stored in the DB.
        """
        raw, hashed = self._generate_verification_token()
        expires = self._utcnow_dt() + timedelta(hours=24)
        await self._s.execute(
            text(
                "UPDATE news_interests "
                "SET pending_email_recipient_token_hash = :hash, "
                "    pending_email_recipient_token_expires = :expires "
                "WHERE id = :iid"
            ),
            {"hash": hashed, "expires": expires, "iid": interest_id},
        )
        await self._s.commit()
        return raw

    async def verify_email_recipient(self, interest_id: int, raw_token: str) -> bool:
        """Verify an email recipient token. Returns True if successful, False if expired/invalid."""
        interest = await self.get_interest_by_id_global(interest_id)
        if not interest:
            return False

        stored_hash = interest.get("pending_email_recipient_token_hash")
        expires = interest.get("pending_email_recipient_token_expires")
        if not stored_hash or not expires:
            return False

        # Check expiry (expires is a naive datetime in UTC)
        now = self._utcnow_dt()
        if expires < now:
            return False

        if self._hash_token(raw_token) != stored_hash:
            return False

        # Mark verified and clear the pending token
        await self._s.execute(
            text(
                "UPDATE news_interests "
                "SET email_recipient_verified = TRUE, "
                "    pending_email_recipient_token_hash = NULL, "
                "    pending_email_recipient_token_expires = NULL "
                "WHERE id = :iid"
            ),
            {"iid": interest_id},
        )
        await self._s.commit()
        return True
