"""Guard test for invariant G2: article URL dedupe holds across runs."""

# version: 0.6.0

from newspipeline.store import parse_rss_date


def _insert(store, run_id, url):
    return store.insert_article(
        run_id=run_id, feed_name="feed", url=url, title="t", author=None,
        published_at="Sat, 08 Aug 2026 06:00:00 +0000", excerpt="e",
        content="c", content_status="full",
    )


def test_article_url_dedupe(store):
    run1 = store.create_run("interest-a")
    run2 = store.create_run("interest-a")

    assert _insert(store, run1["id"], "https://example.com/1") is True
    assert _insert(store, run1["id"], "https://example.com/2") is True
    # Same URL in a later run (within the same interest) is skipped.
    assert _insert(store, run2["id"], "https://example.com/1") is False
    assert _insert(store, run2["id"], "https://example.com/3") is True

    other = store.create_run("interest-b")
    assert _insert(store, other["id"], "https://example.com/1") is True

    assert len(store.get_articles_for_run(run1["id"])) == 2
    assert [a["url"] for a in store.get_articles_for_run(run2["id"])] == ["https://example.com/3"]
    assert store.article_exists("https://example.com/1", run_id=run2["id"]) is True
    assert store.article_exists("https://example.com/nope", run_id=run2["id"]) is False


def test_parse_rss_date_variants():
    # RFC 2822 with zone conversion to UTC
    assert parse_rss_date("Sat, 08 Aug 2026 08:00:00 +0200") == "2026-08-08T06:00:00+00:00"
    # ISO with and without timezone (naive assumed UTC)
    assert parse_rss_date("2026-08-08T06:00:00+00:00") == "2026-08-08T06:00:00+00:00"
    assert parse_rss_date("2026-08-08 06:00:00") == "2026-08-08T06:00:00+00:00"
    # Garbage falls back to "now" rather than raising
    fallback = parse_rss_date("not a date")
    assert fallback.endswith("+00:00") and "T" in fallback
