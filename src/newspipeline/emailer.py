"""SMTP delivery for pipeline results and failure alerts.

Plain-text emails, STARTTLS+LOGIN when a user is configured, plain
connection otherwise (e.g. a local relay). Two retries with 10s backoff on
transient SMTP errors.

The SMTP password comes from .env (NEWS_SMTP_PASSWORD), never from
settings.toml, and is never logged (invariant G1).
"""

# version: 0.1.0

from __future__ import annotations

import logging
import smtplib
import ssl
import time
from datetime import UTC, datetime
from email.mime.text import MIMEText
from typing import Any

from newspipeline.config import get_smtp_password

logger = logging.getLogger(__name__)

_SMTP_MAX_RETRIES = 2
_SMTP_RETRY_BACKOFF_SECONDS = 10


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
    """Send a plain-text email via SMTP."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

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
            send_email(
                **smtp_kwargs,
                subject=f"{name} Daily Brief — {run_date}",
                body=brief.get("content", ""),
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

        parts: list[str] = []
        if summary:
            parts.append(f"=== SUMMARY ===\n{summary}")
        if script:
            parts.append(f"=== SCRIPT (EN) ===\n{script}")
        if script_de:
            parts.append(f"=== SCRIPT (DE) ===\n{script_de}")
        if not parts:
            continue

        send_email(
            **smtp_kwargs,
            subject=f"{name} Theme: {theme.get('title', 'Untitled')} — {run_date}",
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
