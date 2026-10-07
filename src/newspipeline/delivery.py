"""Delivery dispatch: route a finished run's output to file and/or email.

Driven by the interest's ``delivery`` list (``["file"]``, ``["email"]``, or
both). Recipient resolution: the interest's ``email_recipient``, falling
back to the owning recipient email (legacy: ``[user] email``) from settings.toml. Called by the CLI (WP8) and
the scheduler (WP6) after a pipeline run finishes; also provides the
failure-alert path for failed runs.
"""

# version: 0.6.0

from __future__ import annotations

import logging
from pathlib import Path
from datetime import UTC, datetime
import json
from urllib.parse import quote
from newspipeline.timing import next_delivery_at
from typing import Any

from newspipeline import emailer, fileout
from newspipeline.config import PROJECT_ROOT
from newspipeline.store import Store

logger = logging.getLogger(__name__)


def resolve_recipient(interest: dict[str, Any], settings: dict[str, Any]) -> str:
    """Interest override, owning recipient email, or legacy user fallback."""
    return interest.get("email_recipient") or (interest.get("recipient_email", "") if interest.get("recipient_id") else settings.get("user", {}).get("email", ""))


def deliver_run(
    *,
    run_id: int,
    store: Store,
    settings: dict[str, Any],
    interest: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a completed run's output per ``interest["delivery"]``.

    Returns ``{"files": [Path, ...], "emails_sent": int}``. A missing email
    recipient skips email delivery with a warning rather than failing.
    """
    modes = interest.get("delivery") or []
    result: dict[str, Any] = {"files": [], "emails_sent": 0}

    run = store.get_run(run_id)
    if interest.get("recipient_id") and (not run or run["recipient_id"] != interest["recipient_id"] or run["interest_name"] != interest["name"]):
        raise ValueError("run does not belong to this recipient/interest")
    run_date = run.get("run_date", "") if run else ""
    name = interest.get("name", "Interest")

    brief_row = store.get_brief_for_run(run_id)
    brief = brief_row["content"] if brief_row else None
    themes = store.get_themes_for_run(run_id)
    deliverables_by_theme = {
        t["id"]: store.get_deliverables_for_theme(t["id"]) for t in themes
    }

    if "file" in modes:
        output_dir = Path(settings.get("global", {}).get("output_dir", "output"))
        if not output_dir.is_absolute():
            output_dir = PROJECT_ROOT / output_dir
        if interest.get("recipient_id"):
            # Reversible encoding avoids collisions between recipient identifiers.
            output_dir = output_dir / "recipients" / quote(interest["recipient_id"], safe="")
        result["files"] = fileout.write_run_output(
            output_dir=output_dir,
            interest_name=name,
            run_date=run_date,
            brief=brief,
            themes=themes,
            deliverables_by_theme=deliverables_by_theme,
            store=store,
            run_id=run_id,
        )

    if "email" in modes:
        recipient = resolve_recipient(interest, settings)
        if not recipient:
            logger.warning(
                "Email delivery requested but no recipient configured "
                "(interest=%r) — skipping email", name,
            )
        else:
            result["emails_sent"] = emailer.send_pipeline_results(
                run_id=run_id,
                store=store,
                interest=interest,
                smtp_config=settings.get("smtp", {}),
                recipient=recipient,
            )

    return result


def dispatch_finished_run(
    *,
    run_id: int,
    store: Store,
    settings: dict[str, Any],
    interest: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Deliver a finished run's output or send its failure alert.

    Single dispatch path shared by the CLI (`run`) and the scheduler so
    manual and scheduled runs cannot drift apart. Returns ``(status, run)``
    — the row as read for the dispatch decision, so callers never re-read
    a second (possibly divergent) snapshot.
    """
    run = store.get_run(run_id) or {}
    status = run.get("status", "unknown")
    if status == "completed":
        if interest.get("delivery_schedule"):
            due = next_delivery_at(interest["delivery_schedule"], datetime.now(UTC),
                                   settings.get("global", {}).get("timezone", "Europe/Berlin"))
            # Only the non-secret routing/output settings are persisted; the
            # generated output and recipient are bound to this completed run.
            snapshot = {k: v for k, v in interest.items() if k in (
                "name", "recipient_id", "delivery", "email_themes", "enable_summary",
                "enable_script", "enable_script_de", "enable_brief",
            )}
            snapshot["email_recipient"] = resolve_recipient(interest, settings)
            payload = {
                "interest": snapshot,
                "settings": {
                    "global": {"output_dir": settings.get("global", {}).get("output_dir", "output")},
                    "smtp": {k: v for k, v in settings.get("smtp", {}).items() if k in ("host", "port", "user", "sender")},
                    "user": {},
                },
            }
            store.queue_delivery(run_id, due.isoformat(timespec="seconds"), payload)
            run = {**run, "delivery_status": "pending", "delivery_due_at": due.isoformat()}
        else:
            deliver_run(run_id=run_id, store=store, settings=settings, interest=interest)
    else:
        deliver_failure_alert(
            interest=interest,
            settings=settings,
            stage_name=run.get("current_stage") or "unknown",
            error_message=run.get("error") or "unknown error",
        )
    return status, run


def deliver_failure_alert(
    *,
    interest: dict[str, Any],
    settings: dict[str, Any],
    stage_name: str,
    error_message: str,
) -> bool:
    """Send a failure-alert email for a failed run. Returns True if sent.

    Alerts go out regardless of the interest's ``delivery`` modes — a failed
    run produces nothing to deliver, so the mode list does not apply. A
    missing recipient skips the alert with a warning.
    """
    recipient = resolve_recipient(interest, settings)
    if not recipient:
        logger.warning(
            "Failure alert not sent — no recipient configured (interest=%r)",
            interest.get("name"),
        )
        return False
    try:
        emailer.send_failure_alert(
            interest_name=interest.get("name", "Interest"),
            stage_name=stage_name,
            error_message=error_message,
            smtp_config=settings.get("smtp", {}),
            recipient=recipient,
        )
        return True
    except emailer.EmailerError as exc:
        logger.error("Failure alert could not be sent: %s", exc)
        return False


def deliver_due(store: Store, now: datetime | None = None) -> int:
    """Drain due jobs off the scheduler loop. A failed send is visible and
    requires an explicit retry; automatic retries cannot multiply emails.
    """
    now = now or datetime.now(UTC)
    delivered = 0
    while job := store.claim_due_delivery(now.astimezone(UTC).isoformat(timespec="seconds")):
        try:
            payload = json.loads(job["payload"])
            deliver_run(run_id=job["run_id"], store=store, **payload)
        except Exception as exc:
            logger.exception("Scheduled delivery failed — run=%s", job["run_id"])
            store.finish_delivery(job["run_id"], str(exc))
        else:
            store.finish_delivery(job["run_id"])
            delivered += 1
    return delivered
