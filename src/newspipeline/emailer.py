"""SMTP delivery for pipeline results and failure alerts.

Emails are sent as HTML: the pipeline produces markdown deliverables, which
are converted with the `markdown` package (with the `fenced_code` and
`sane_lists` extensions) so Gmail renders them properly. Local file output
stays markdown (see fileout.py). STARTTLS+LOGIN when a user is configured,
plain connection otherwise (e.g. a local relay). Two retries with 10s
backoff on transient SMTP errors.

The SMTP password comes from .env (NEWS_SMTP_PASSWORD), never from
settings.toml, and is never logged (invariant G1).
"""

# version: 0.4.0

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import time
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import markdown

from newspipeline.config import get_smtp_password

logger = logging.getLogger(__name__)

_SMTP_MAX_RETRIES = 2
_SMTP_RETRY_BACKOFF_SECONDS = 10

_MD_EXTENSIONS = ["fenced_code", "sane_lists"]

# Minimal, email-client-friendly stylesheet: email CSS support is spotty,
# so only basic block-level styling that Gmail's web UI honors.
_CSS = """\
body { font-family: -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
       font-size: 15px; line-height: 1.55; color: #1a1a1a; margin: 1em; }
h1 { font-size: 1.35em; margin: 1.1em 0 0.4em; }
h2 { font-size: 1.15em; margin: 1em 0 0.35em; border-bottom: 1px solid #ddd; padding-bottom: 0.15em; }
h3 { font-size: 1.02em; margin: 0.9em 0 0.3em; }
p { margin: 0.55em 0; }
ul, ol { margin: 0.55em 0; padding-left: 1.5em; }
li { margin: 0.2em 0; }
a { color: #0b57d0; }
hr { border: none; border-top: 1px solid #ddd; margin: 1.4em 0; }
code { background: #f2f2f2; padding: 0.1em 0.3em; border-radius: 3px; }
pre { background: #f2f2f2; padding: 0.7em; overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote { border-left: 3px solid #ccc; margin: 0.7em 0; padding: 0.1em 0.9em; color: #444; }
"""


def markdown_to_html(text: str) -> str:
    """Convert markdown text to a standalone HTML fragment for email bodies."""
    body = markdown.markdown(text, extensions=_MD_EXTENSIONS)
    return (
        "<html><head><meta charset='utf-8'>"
        f"<style>{_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


def build_source_links(
    *,
    run_id: int,
    source_article_ids: list[int],
    store: Any,
) -> list[dict[str, str]]:
    """Resolve a theme's 1-based source article indices to title/url pairs.

    Unresolvable indices are skipped. Used for both theme emails and the
    daily brief email so links render consistently.
    """
    links: list[dict[str, str]] = []
    for art in store.get_articles_by_ids(run_id, source_article_ids):
        url = art.get("url") or ""
        if not url:
            continue
        links.append({"title": art.get("title") or url, "url": url})
    return links


def _format_links_section(name: str, links: list[dict[str, str]]) -> str:
    """Render a 'Sources' block as markdown: '1. [Title](url)' per line."""
    if not links:
        return ""
    lines = [f"Sources ({name}):"]
    for i, link in enumerate(links, 1):
        title = link["title"].replace("[", "(").replace("]", ")")
        lines.append(f"{i}. [{title}]({link['url']})")
    return "\n".join(lines)


class EmailerError(Exception):
    """Raised when an email cannot be sent."""


def send_email(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
) -> None:
    """Send an HTML email via SMTP; `body` is markdown, rendered to HTML."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(markdown_to_html(body), "html", "utf-8"))

    last_exc: Exception | None = None
    wants_auth = bool(smtp_user)
    for attempt in range(_SMTP_MAX_RETRIES + 1):
        try:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                if wants_auth:
                    # smtplib's default STARTTLS context does NOT verify
                    # certificates — pass a verifying one so credentials
                    # cannot be harvested by an active MITM.
                    server.starttls(context=ssl.create_default_context())
                    server.login(smtp_user, smtp_password)
                server.send_message(msg)
            logger.info("Email sent — subject=%r recipient=%s", subject, recipient)
            return
        except (smtplib.SMTPException, OSError) as exc:
            last_exc = exc
            if attempt < _SMTP_MAX_RETRIES:
                logger.warning(
                    "SMTP error (attempt %d/%d) — retrying in %ds: %s",
                    attempt + 1, _SMTP_MAX_RETRIES + 1,
                    _SMTP_RETRY_BACKOFF_SECONDS, exc,
                )
                time.sleep(_SMTP_RETRY_BACKOFF_SECONDS)

    raise EmailerError(
        f"Failed to send email after {_SMTP_MAX_RETRIES + 1} attempts: {last_exc}"
    ) from last_exc


def send_pipeline_results(
    *,
    run_id: int,
    store: Any,
    interest: dict[str, Any],
    smtp_config: dict[str, Any],
    recipient: str,
) -> int:
    """Send the daily brief and per-theme deliverable emails for a run.

    One brief email when ``enable_brief``, then one email per theme
    containing the enabled deliverables (summary / script / script_de).
    Returns the number of emails sent.
    """
    emails_sent = 0

    run = store.get_run(run_id)
    run_date = run.get("run_date", "") if run else ""
    name = interest.get("name", "Interest")

    smtp_kwargs: dict[str, Any] = {
        "smtp_host": smtp_config.get("host", "localhost"),
        "smtp_port": smtp_config.get("port", 25),
        "smtp_user": smtp_config.get("user", ""),
        "smtp_password": get_smtp_password(),
        "sender": smtp_config.get("sender", "newspipeline@localhost"),
        "recipient": recipient,
    }

    if interest.get("enable_brief"):
        brief = store.get_brief_for_run(run_id)
        if brief:
            # The brief covers every theme in the run — collect the union of
            # all themes' source links, then dedupe by URL, preserving order.
            seen: set[str] = set()
            brief_links: list[dict[str, str]] = []
            for theme in store.get_themes_for_run(run_id):
                ids = theme.get("source_article_ids") or []
                if isinstance(ids, str):
                    ids = json.loads(ids)
                for link in build_source_links(
                    run_id=run_id, source_article_ids=ids, store=store
                ):
                    if link["url"] not in seen:
                        seen.add(link["url"])
                        brief_links.append(link)

            brief_parts = [brief.get("content", "")]
            if brief_links:
                brief_parts.append(_format_links_section(name, brief_links))
            send_email(
                **smtp_kwargs,
                subject=f"{name} Daily Brief — {run_date}",
                body="\n\n".join(brief_parts),
            )
            emails_sent += 1

    for theme in store.get_themes_for_run(run_id):
        summary = script = script_de = None
        for d in store.get_deliverables_for_theme(theme.get("id", 0)):
            dtype = d.get("deliverable_type", "")
            if dtype == "summary" and interest.get("enable_summary"):
                summary = d.get("content")
            elif dtype == "script" and interest.get("enable_script"):
                script = d.get("content")
            elif dtype == "script_de" and interest.get("enable_script_de"):
                script_de = d.get("content")
        if not (summary or script or script_de):
            continue

        theme_name = theme.get("title", "Untitled")
        ids = theme.get("source_article_ids") or []
        if isinstance(ids, str):
            ids = json.loads(ids)
        links = build_source_links(run_id=run_id, source_article_ids=ids, store=store)
        links_section = _format_links_section(theme_name, links)

        parts: list[str] = []
        if summary:
            parts.append(f"## Summary\n\n{summary}")
        if script:
            parts.append(f"## Script (EN)\n\n{script}")
        if script_de:
            parts.append(f"## Script (DE)\n\n{script_de}")
        if links_section:
            parts.append(links_section)

        send_email(
            **smtp_kwargs,
            subject=f"{name} Theme: {theme_name} — {run_date}",
            body="\n\n".join(parts),
        )
        emails_sent += 1

    logger.info(
        "Pipeline result emails sent — %d email(s) for run %d, interest=%r",
        emails_sent, run_id, name,
    )
    return emails_sent


def send_failure_alert(
    *,
    interest_name: str,
    stage_name: str,
    error_message: str,
    smtp_config: dict[str, Any],
    recipient: str,
) -> None:
    """Send a failure-alert email for a pipeline error."""
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    send_email(
        smtp_host=smtp_config.get("host", "localhost"),
        smtp_port=smtp_config.get("port", 25),
        smtp_user=smtp_config.get("user", ""),
        smtp_password=get_smtp_password(),
        sender=smtp_config.get("sender", "newspipeline@localhost"),
        recipient=recipient,
        subject=f"{interest_name} Pipeline FAILURE — {stage_name} — {date}",
        body=f"Stage: {stage_name}\nError: {error_message}\n",
    )
    logger.info(
        "Failure alert email sent — interest=%r stage=%s date=%s",
        interest_name, stage_name, date,
    )
