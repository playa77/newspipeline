"""SMTP delivery of report documents, plus failure alerts.

Every email is rendered from a `document.Document`: the HTML part is the
designed artifact (email_layout.render_html), the text part is the same
document rendered as text (email_layout.render_text). No markdown is
converted on the way to an inbox — see DECISIONS.md 2026-09-16, "HTML is
first-class output".

The SMTP password comes from .env (NEWS_SMTP_PASSWORD), never from
settings.toml, and is never logged (invariant G1).
"""

# version: 0.5.0

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import time
from dataclasses import replace
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from newspipeline import compose
from newspipeline.config import get_smtp_password
from newspipeline.document import Document, Paragraph, Section
from newspipeline.email_layout import render_html, render_text

logger = logging.getLogger(__name__)

_SMTP_MAX_RETRIES = 2
_SMTP_RETRY_BACKOFF_SECONDS = 10


class EmailerError(Exception):
    """Raised when an email cannot be sent."""


def send_email(
    *,
    document: Document,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    sender: str,
    recipient: str,
) -> None:
    """Send one report email: the document rendered as text and as HTML.

    The subject travels on the document (`Document.subject`) — the email is
    a pure render of the document, with nothing added at transport time.
    The text part comes first: RFC 2046 orders multipart/alternative parts by
    increasing faithfulness and clients show the last part they support.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = document.subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(render_text(document), "plain", "utf-8"))
    msg.attach(MIMEText(render_html(document), "html", "utf-8"))

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
            logger.info(
                "Email sent — subject=%r recipient=%s", document.subject, recipient
            )
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


def _stored_documents(store: Any, theme_id: int) -> dict[str, Document]:
    """Every deliverable stored for a theme, parsed into documents."""
    documents: dict[str, Document] = {}
    for row in store.get_deliverables_for_theme(theme_id):
        documents[row.get("deliverable_type", "")] = compose.load_document(
            row.get("content", "")
        )
    return documents


def _framed(document: Document, *, subject: str, preheader: str = "") -> Document:
    """Attach the transport-level framing the renderers do not own."""
    return replace(
        document,
        subject=subject,
        preheader=document.preheader or preheader,
    )


def send_pipeline_results(
    *,
    run_id: int,
    store: Any,
    interest: dict[str, Any],
    smtp_config: dict[str, Any],
    recipient: str,
) -> int:
    """Send the daily brief and per-theme report emails for a run.

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

    themes = store.get_themes_for_run(run_id)

    if interest.get("enable_brief"):
        brief = store.get_brief_for_run(run_id)
        if brief:
            # The brief document already carries each topic's own sources
            # (compose.brief_document) — the email is a pure render of it.
            document = compose.load_document(brief.get("content", ""), interest_name=name)
            send_email(
                **smtp_kwargs,
                document=_framed(
                    document,
                    subject=f"{name} Daily Brief — {run_date}",
                    preheader=document.deck,
                ),
            )
            emails_sent += 1

    for position, theme in enumerate(themes, 1):
        documents = _stored_documents(store, theme.get("id", 0))
        summary = documents.get("summary") if interest.get("enable_summary") else None
        script = documents.get("script") if interest.get("enable_script") else None
        script_de = (
            documents.get("script_de") if interest.get("enable_script_de") else None
        )
        if not (summary or script or script_de):
            continue

        ids = theme.get("source_article_ids") or []
        if isinstance(ids, str):
            ids = json.loads(ids)
        theme_name = theme.get("title", "Untitled")
        document = compose.theme_document(
            interest_name=name,
            theme=theme,
            position=position,
            total=len(themes),
            summary=summary,
            script=script,
            script_de=script_de,
            sources=compose.resolve_sources(store, run_id, ids),
        )
        send_email(
            **smtp_kwargs,
            document=_framed(
                document,
                subject=f"{name} Theme: {theme_name} — {run_date}",
                preheader=theme.get("description", ""),
            ),
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
    document = Document(
        kicker="newspipeline · failure alert",
        headline=f"Pipeline failure — {stage_name}",
        deck=f"The run for the interest '{interest_name}' failed during the "
        f"{stage_name} stage on {date}.",
        sections=(
            Section(heading="Error", blocks=(Paragraph(error_message),)),
        ),
        footer_note=compose.footer_note(interest_name),
        subject=f"{interest_name} Pipeline FAILURE — {stage_name} — {date}",
    )
    send_email(
        document=document,
        smtp_host=smtp_config.get("host", "localhost"),
        smtp_port=smtp_config.get("port", 25),
        smtp_user=smtp_config.get("user", ""),
        smtp_password=get_smtp_password(),
        sender=smtp_config.get("sender", "newspipeline@localhost"),
        recipient=recipient,
    )
    logger.info(
        "Failure alert email sent — interest=%r stage=%s date=%s",
        interest_name, stage_name, date,
    )
