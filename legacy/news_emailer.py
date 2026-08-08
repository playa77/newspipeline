"""News Emailer — SMTP dispatch for pipeline success and failure notifications.

Adapted from ai_news_scraper/src/emailer.py.  Sends plain-text emails via SMTP
(Gmail App Password flow).  Internal retries on transient SMTP errors.
"""

from __future__ import annotations

import logging
import os
import smtplib
import time
from email.mime.text import MIMEText
from typing import Any

logger = logging.getLogger(__name__)

_SMTP_MAX_RETRIES = 2
_SMTP_RETRY_BACKOFF_SECONDS = 10


class EmailerError(Exception):
    """Raised when an email cannot be sent."""


def _send_email(
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
    """Send a plain-text email via SMTP.

    Connects to smtp_host:smtp_port.  If smtp_user is provided,
    STARTTLS + LOGIN is attempted; otherwise a plain (unauthenticated)
    connection is used (e.g. for a local Postfix relay).
    """
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
            logger.info("Email sent — subject=%r", subject)
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


async def send_failure_alert(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    sender: str,
    recipient: str,
    interest_name: str,
    stage_name: str,
    error_message: str,
    traceback_str: str,
) -> None:
    """Send a failure-alert email for a pipeline stage error."""
    from datetime import datetime

    date = datetime.now().strftime("%Y-%m-%d")
    subject = f"{interest_name} Pipeline FAILURE — {stage_name} — {date}"
    body = (
        f"Stage: {stage_name}\n"
        f"Error: {error_message}\n\n"
        f"Traceback:\n{traceback_str}"
    )
    _send_email(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        sender=sender,
        recipient=recipient,
        subject=subject,
        body=body,
    )
    logger.info("Failure alert email sent — stage=%s date=%s", stage_name, date)


async def send_success_brief(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    sender: str,
    recipient: str,
    interest_name: str,
    run_date: str,
    brief_content: str,
) -> None:
    """Send the daily brief email."""
    subject = f"{interest_name} Daily Brief — {run_date}"
    _send_email(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        sender=sender,
        recipient=recipient,
        subject=subject,
        body=brief_content,
    )


async def send_success_theme(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    sender: str,
    recipient: str,
    interest_name: str,
    run_date: str,
    theme_title: str,
    summary: str | None = None,
    script: str | None = None,
    script_de: str | None = None,
) -> None:
    """Send a per-theme deliverable email with summary and scripts."""
    parts: list[str] = []
    if summary:
        parts.append(f"=== SUMMARY ===\n{summary}")
    if script:
        parts.append(f"=== SCRIPT (EN) ===\n{script}")
    if script_de:
        parts.append(f"=== SCRIPT (DE) ===\n{script_de}")
    if not parts:
        return

    subject = f"{interest_name} Theme: {theme_title} — {run_date}"
    _send_email(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        sender=sender,
        recipient=recipient,
        subject=subject,
        body="\n\n".join(parts),
    )


async def send_pipeline_results(
    *,
    run_id: int,
    store: Any,
    interest: dict[str, Any],
    smtp_config: dict[str, Any],
) -> int:
    """Send all deliverable emails for a completed pipeline run.

    Returns count of emails sent.
    """
    emails_sent = 0

    run = await store.get_run(run_id)
    run_date = run.get("run_date", "") if run else ""
    name = interest.get("name", "Interest")

    smtp_password = os.environ.get("WORKBENCH_NEWS_SMTP_PASSWORD", "") or smtp_config.get("password", "")

    smtp_kwargs = {
        "smtp_host": smtp_config.get("host", "172.18.0.1"),
        "smtp_port": smtp_config.get("port", 25),
        "smtp_user": smtp_config.get("user", ""),
        "smtp_password": smtp_password,
        "sender": smtp_config.get("sender", "emailfrom@workbench.gronowski.cc"),
        "recipient": smtp_config.get("recipient", ""),
    }

    # Daily brief
    if interest.get("enable_brief"):
        brief = await store.get_daily_brief_for_run(run_id)
        if brief:
            await send_success_brief(
                **smtp_kwargs,
                interest_name=name,
                run_date=run_date,
                brief_content=brief.get("content", ""),
            )
            emails_sent += 1

    # Per-theme deliverables
    themes = await store.get_themes_for_run(run_id)
    for theme in themes:
        # Fetch deliverables for this theme
        summary = None
        script = None
        script_de = None

        deliverables = await store.get_deliverables_for_theme(theme.get("id", 0))
        for d in deliverables:
            dtype = d.get("deliverable_type", "")
            if dtype == "summary" and interest.get("enable_summary"):
                summary = d.get("content")
            elif dtype == "script" and interest.get("enable_script"):
                script = d.get("content")
            elif dtype == "script_de" and interest.get("enable_script_de"):
                script_de = d.get("content")

        if summary or script or script_de:
            await send_success_theme(
                **smtp_kwargs,
                interest_name=name,
                run_date=run_date,
                theme_title=theme.get("title", "Untitled"),
                summary=summary,
                script=script,
                script_de=script_de,
            )
            emails_sent += 1

    logger.info("Pipeline result emails sent — %d email(s) for run %d", emails_sent, run_id)
    return emails_sent
