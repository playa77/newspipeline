"""Migration preserves history; SQLite enforces interest-scoped isolation."""

# version: 0.6.0

import json
from pathlib import Path
import sqlite3
import pytest
from newspipeline.store import Store
from test_store import _insert


def test_same_url_dedupes_only_within_recipient_and_interest(store):
    first = store.create_run("Energy", "alice")
    repeat = store.create_run("Energy", "alice")
    other_interest = store.create_run("Space", "alice")
    other_recipient = store.create_run("Energy", "bob")
    for run in (first, other_interest, other_recipient):
        assert _insert(store, run["id"], "https://example.com/1")
    assert not _insert(store, repeat["id"], "https://example.com/1")
    assert store.article_exists("https://example.com/1", run_id=repeat["id"])
    assert not store.article_exists("https://example.com/2", run_id=repeat["id"])
    assert len({r["scope_id"] for r in (first, repeat, other_interest, other_recipient)}) == 3
    assert store.is_interest_running("Energy", "bob")
    store.update_run(other_recipient["id"], status="completed")
    assert not store.is_interest_running("Energy", "bob")
    assert store.is_interest_running("Energy", "alice")


def test_theme_groups_cannot_link_articles_from_other_runs_or_interests(store):
    a, b = store.create_run("Energy", "alice"), store.create_run("Energy", "bob")
    _insert(store, a["id"], "https://example.com/a")
    _insert(store, b["id"], "https://example.com/b")
    theme = store.insert_theme(run_id=a["id"], title="Group", description="related stories", source_article_ids=[1, 1, 99, "1"], order_index=0)
    assert json.loads(theme["source_article_ids"]) == [1]
    article_b = store.get_articles_for_run(b["id"])[0]
    with pytest.raises(sqlite3.IntegrityError):
        store._execute("INSERT INTO theme_articles VALUES (?,?,?)", (theme["id"], article_b["id"], a["id"]))
    with pytest.raises(sqlite3.IntegrityError):
        store._execute("UPDATE articles SET scope_id=? WHERE id=?", (a["scope_id"], article_b["id"]))
    assert store._execute("PRAGMA foreign_key_check").fetchall() == []


def legacy_database(path):
    db = sqlite3.connect(path)
    db.executescript((Path(__file__).parent / "fixtures/schema_v05.sql").read_text())
    db.execute("INSERT INTO runs VALUES (7,'Energy','completed','brief',NULL,'2026-10-01','2026-10-01T00:00:00+00:00',NULL)")
    db.execute("INSERT INTO articles VALUES (9,7,'feed','https://example.com/a','title',NULL,'2026-10-01','2026-10-01','excerpt','body','full')")
    db.execute("INSERT INTO themes VALUES (3,7,'Group','related','[1]',0)")
    db.execute("INSERT INTO deliverables VALUES (4,3,'summary','preserved summary')")
    db.execute("INSERT INTO briefs VALUES (5,7,'preserved brief',2)")
    db.commit()
    db.close()


def test_legacy_schema_migration_preserves_ids_history_output_and_links(tmp_path):
    path = tmp_path / "old.db"
    legacy_database(path)
    with_store = Store(path)
    try:
        assert with_store.get_run(7)["recipient_id"] == ""
        assert with_store.get_articles_for_run(7)[0]["id"] == 9
        assert with_store.get_deliverables_for_theme(3)[0]["content"] == "preserved summary"
        assert with_store.get_brief_for_run(7)["content"] == "preserved brief"
        assert tuple(with_store._execute("SELECT * FROM theme_articles").fetchone()) == (3, 9, 7)
        repeat = with_store.create_run("Energy")
        assert repeat["id"] > 7
        assert not _insert(with_store, repeat["id"], "https://example.com/a")
        other = with_store.create_run("Space")
        assert _insert(with_store, other["id"], "https://example.com/a")
        with_store.update_run(repeat["id"], status="completed")
        with_store.rename_interest("Energy", "Power")
        assert with_store.get_run(7)["interest_name"] == "Power"
        renamed = with_store.create_run("Power")
        assert not _insert(with_store, renamed["id"], "https://example.com/a")
        assert with_store._execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        with_store.close()
    reopened = Store(path)
    assert reopened.get_run(7)["interest_name"] == "Power"
    reopened.close()


def test_migration_rolls_back_when_legacy_foreign_keys_are_corrupt(tmp_path):
    path = tmp_path / "broken.db"
    legacy_database(path)
    db = sqlite3.connect(path)
    db.execute("UPDATE deliverables SET theme_id=999")
    db.commit()
    db.close()
    with pytest.raises(ValueError, match="foreign keys"):
        Store(path)
    db = sqlite3.connect(path)
    assert "scope_id" not in {r[1] for r in db.execute("PRAGMA table_info(runs)")}
    assert db.execute("SELECT count(*) FROM articles").fetchone()[0] == 1
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='interest_scopes'").fetchall()
    db.close()
