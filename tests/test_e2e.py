"""Guard test for invariant G5: an end-to-end pipeline run against a fixture
RSS feed with a mocked LLM endpoint produces themes, deliverables, a brief,
and file output. Plus an end-to-end email-delivery test against a fake SMTP
server (unauthenticated path only — authenticated SMTP is T2).
"""

# version: 0.5.0

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
    assert "# Fixture summary headline" in theme_md
    assert "## Summary" in theme_md
    assert "## Script (English)" in theme_md and "## Script (German)" in theme_md
    brief_md = (result["files"][0]).read_text()
    assert "# Fixture brief headline" in brief_md
    assert "WHAT TO WATCH" not in brief_md  # headings keep their case in markdown
    assert "## What to watch" in brief_md
    # The archive carries each topic's links under that topic, like the email.
    assert brief_md.count("**Sources**") == 2
    assert "http://127.0.0.1" in brief_md

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
    assert any("SUMMARY" in b and "SCRIPT (ENGLISH)" in b for b in bodies)
    assert all("SCRIPT (GERMAN)" not in b for b in bodies)  # script_de disabled
    assert all("e2e-key" not in b for b in bodies)  # no secret leakage
    # HTML part: the designed layout, not a text conversion.
    assert all("<html" in h and "prefers-color-scheme" in h for h in htmls)
    assert any(">Summary</h2>" in h and ">Script (English)</h2>" in h for h in htmls), htmls[0]
    assert all('role="presentation"' in h for h in htmls), htmls[0]
    assert all('href="http://127.0.0.1' in h for h in htmls), htmls[0]

    # A theme report carries its own theme's sources…
    theme_parsed = [(s, b, h) for (s, b, h) in parsed if "Theme:" in s]
    assert len(theme_parsed) == 2
    for body, html in [(b, h) for _, b, h in theme_parsed]:
        assert "Sources:" in body and "http://127.0.0.1" in body, body
        assert 'href="http://127.0.0.1' in html, html

    # …and the brief attributes each topic's links to that topic: topic one's
    # articles sit under topic one, topic two's under topic two. Article a1
    # belongs to both themes, so it appears under both — that is attribution,
    # not a duplicated pool list.
    brief_body = next(b for s, b, _ in parsed if "Daily Brief" in s)
    assert "WHAT TO WATCH" in brief_body
    first = brief_body.index("FIXTURE TOPIC 1")
    second = brief_body.index("FIXTURE TOPIC 2")
    assert first < second
    assert "/a1" in brief_body[first:second] and "/a2" in brief_body[first:second]
    assert "/a1" in brief_body[second:] and "/a2" not in brief_body[second:]


def test_email_themes_off_mails_only_the_brief(
    isolated_config, fixture_server, smtp_server, store, tmp_path,
):
    """`email_themes = false`: the brief is the interest's only mail.

    The per-theme emails are suppressed, but nothing else changes — the
    themes and their deliverables are still generated, stored and written to
    the file archive (that is what makes this different from switching the
    summary deliverable off entirely)."""
    settings = make_settings(
        fixture_server.server_port, tmp_path / "output",
        smtp={"host": "127.0.0.1", "port": smtp_server.server_address[1]},
        user={"email": "fallback@example.com"},
    )
    interest = make_interest(
        fixture_server.server_port, ["file", "email"],
        enable_script_de=False, email_themes=False,
    )

    run_id = _run_pipeline(store, settings, interest)
    assert store.get_run(run_id)["status"] == "completed"

    themes = store.get_themes_for_run(run_id)
    assert len(themes) == 2
    for theme in themes:
        kinds = {d["deliverable_type"] for d in store.get_deliverables_for_theme(theme["id"])}
        assert "summary" in kinds, kinds

    result = deliver_run(run_id=run_id, store=store, settings=settings, interest=interest)
    assert result["emails_sent"] == 1
    assert len(smtp_server.captured_emails) == 1
    msg = message_from_string(smtp_server.captured_emails[0]["data"])
    assert "Daily Brief" in str(make_header(decode_header(msg["Subject"])))

    files = sorted(p.name for p in result["files"])
    assert files == [
        "brief.md",
        "theme-01-renewable-energy-surge.md",
        "theme-02-grid-storage-policy.md",
    ]
