"""SMTP delivery for pipeline results and failure alerts.

Ported from legacy/news_emailer.py: plain-text emails, STARTTLS+LOGIN when a
user is configured, plain connection otherwise (e.g. a local relay). Two
retries with 10s backoff on transient SMTP errors.

The SMTP password comes from .env (NEWS_SMTP_PASSWORD), never from
settings.toml, and is never logged (invariant G1).
"""

# version: 0.1.0

from __future__ import annotations

import logging
import smtplib
import time
from email.mime.text import MIMEText
from typing import Any

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
    """Send a plain-text email via SMTP (ported from legacy _send_email)."""
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
                    server.starttls()
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

    Returns the number of emails sent.

    TODO(WP5): implement — port from legacy send_pipeline_results:
    brief email when enable_brief, one email per theme containing the enabled
    deliverables (summary / script / script_de).
    """
    raise NotImplementedError("WP5")


def send_failure_alert(
    *,
    interest_name: str,
    stage_name: str,
    error_message: str,
    smtp_config: dict[str, Any],
    recipient: str,
) -> None:
    """Send a failure-alert email for a pipeline error.

    TODO(WP5): implement — port from legacy send_failure_alert.
    """
    raise NotImplementedError("WP5")
