-- Historical schema for migration regression tests.

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
