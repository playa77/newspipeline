"""Guard test for invariant G5: an end-to-end pipeline run against a fixture
RSS feed with a mocked LLM endpoint produces themes, deliverables, a brief,
and file output. Plus an end-to-end email-delivery test against a fake SMTP
server (unauthenticated path only — authenticated SMTP is T2).
"""

# version: 0.3.0

import asyncio
from email import message_from_string
from email.header import decode_header, make_header

from newspipeline.delivery import deliver_run
from newspipeline.pipeline import Pipeline

from conftest import THEMES, make_interest, make_settings


def _run_pipeline(store, settings, interest, api_key="e2e-key"):
    pipeline = Pipeline(store=store, settings=settings, api_key=api_key)
    return asyncio.run(pipeline.run(interest))


def test_pipeline_end_to_end(isolated_config, fixture_server, store, tmp_path):
    settings = make_settings(fixture_server.server_port, tmp_path / "output")
    interest = make_interest(fixture_server.server_port, ["file"])

    run_id = _run_pipeline(store, settings, interest)
    run = store.get_run(run_id)
    assert run["status"] == "completed"
    assert run["completed_at"]

    # Articles scraped with full-content extraction and parsed RSS dates.
    articles = store.get_articles_for_run(run_id)
    assert len(articles) == 2
    assert all(a["content_status"] == "full" and a["content"] for a in articles)
    assert {a["published_at"] for a in articles} == {
        "2026-08-08T06:00:00+00:00", "2026-08-08T07:30:00+00:00",
    }

    # Themes from the mocked analysis call.
    themes = store.get_themes_for_run(run_id)
    assert [t["title"] for t in themes] == [t["title"] for t in THEMES]

    # All three deliverable types per theme.
    for theme in themes:
        kinds = {d["deliverable_type"] for d in store.get_deliverables_for_theme(theme["id"])}
        assert kinds == {"summary", "script", "script_de"}, kinds

    # Brief stored with a word count.
    brief = store.get_brief_for_run(run_id)
    assert brief and brief["content"] and brief["word_count"] > 0

    # File delivery: brief + one markdown file per theme.
    result = deliver_run(run_id=run_id, store=store, settings=settings, interest=interest)
    files = sorted(p.name for p in result["files"])
    assert files == [
        "brief.md",
        "theme-01-renewable-energy-surge.md",
        "theme-02-grid-storage-policy.md",
    ]
    assert all(p.parent.parent.name == "guard-fixture" for p in result["files"])
    theme_md = (result["files"][1]).read_text()
    assert "## Summary" in theme_md and "## Script (EN)" in theme_md and "## Script (DE)" in theme_md
    assert "Generated fixture content" in (result["files"][0]).read_text()

    # URL dedupe across runs: a second run scrapes nothing new.
    run_id_2 = _run_pipeline(store, settings, interest)
    assert store.get_run(run_id_2)["status"] == "completed"
    assert store.get_articles_for_run(run_id_2) == []


def test_email_delivery_end_to_end(isolated_config, fixture_server, smtp_server, store, tmp_path):
    settings = make_settings(
        fixture_server.server_port, tmp_path / "output",
        smtp={"host": "127.0.0.1", "port": smtp_server.server_address[1]},
        user={"email": "fallback@example.com"},
    )
    interest = make_interest(fixture_server.server_port, ["email"], enable_script_de=False)

    run_id = _run_pipeline(store, settings, interest)
    assert store.get_run(run_id)["status"] == "completed"

    result = deliver_run(run_id=run_id, store=store, settings=settings, interest=interest)
    assert result["emails_sent"] == 3  # 1 brief + 2 theme emails
    assert result["files"] == []

    emails = smtp_server.captured_emails
    assert len(emails) == 3
    assert all(e["rcpt"] == ["fallback@example.com"] for e in emails)

    def decoded(raw):
        msg = message_from_string(raw)
        subject = str(make_header(decode_header(msg["Subject"])))
        # Emails are multipart/alternative: plain first, HTML last.
        assert msg.is_multipart() and msg.get_content_type() == "multipart/alternative"
        parts = list(msg.walk())
        plain = next(p for p in parts if p.get_content_type() == "text/plain")
        html = next(p for p in parts if p.get_content_type() == "text/html")
        return subject, plain.get_payload(decode=True).decode(), html.get_payload(decode=True).decode()

    parsed = [decoded(e["data"]) for e in emails]
    subjects = [s for s, _, _ in parsed]
    assert any("Daily Brief" in s for s in subjects), subjects
    assert sum("Theme:" in s for s in subjects) == 2, subjects
    bodies = [b for _, b, _ in parsed]
    htmls = [h for _, _, h in parsed]
    assert any("## Summary" in b and "## Script (EN)" in b for b in bodies)
    assert all("## Script (DE)" not in b for b in bodies)  # script_de disabled
    assert all("e2e-key" not in b for b in bodies)  # no secret leakage
    # HTML part: same content, properly rendered (headings, linked sources).
    assert all("<html>" in h and "<style>" in h for h in htmls)
    assert any("<h2>Summary</h2>" in h and "<h2>Script (EN)</h2>" in h for h in htmls), htmls[0]
    assert all('href="http://127.0.0.1' in h for h in htmls), htmls[0]

    # Source-article links: every theme email carries its theme's sources;
    # the brief email carries the deduplicated union of all sources.
    theme_parsed = [(s, b, h) for (s, b, h) in parsed if "Theme:" in s]
    assert len(theme_parsed) == 2
    for body, html in [(b, h) for _, b, h in theme_parsed]:
        assert "Sources (Renewable Energy Surge):" in body or \
               "Sources (Grid Storage Policy):" in body, body
        assert "http://127.0.0.1" in body, body
        assert 'href="http://127.0.0.1' in html, html
    brief = next((s, b, h) for (s, b, h) in parsed if "Daily Brief" in s)
    assert "Sources (guard-fixture):" in brief[1]
    # Union of sources: article a1 appears in both themes but only once in the brief.
    assert brief[1].count("http://127.0.0.1") == 2
