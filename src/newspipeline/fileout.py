"""Local-file delivery: the markdown archive.

The archive is a sibling rendering of the same documents the emails are
built from (`document.Document.to_markdown`) — the HTML is not derived from
these files and these files are not derived from the HTML. Files carry the
same per-topic source lists as the emails, resolved from the store.

Output layout:

    <output_dir>/<interest-slug>/<run-date>/
        brief.md
        theme-01-<slug>.md   (summary / scripts, one file per theme)
"""

# version: 0.2.0

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from newspipeline import compose

logger = logging.getLogger(__name__)


def slugify(text: str) -> str:
    """Filesystem-safe slug: lowercase, alphanumerics and dashes only."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "untitled"


def _documents_by_type(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        row.get("deliverable_type", ""): compose.load_document(row.get("content", ""))
        for row in rows
    }


def write_run_output(
    *,
    output_dir: Path | str,
    interest_name: str,
    run_date: str,
    brief: str | None,
    themes: list[dict[str, Any]],
    deliverables_by_theme: dict[int, list[dict[str, Any]]],
    store: Any,
    run_id: int,
) -> list[Path]:
    """Write one run's brief and theme reports as markdown.

    ``brief`` is the stored brief document (JSON, or None); the theme files
    are assembled exactly as the theme emails are, so the archive and the
    inbox can never disagree about what a run produced. Returns the written
    paths.
    """
    base = Path(output_dir) / slugify(interest_name) / run_date
    base.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []

    if brief:
        path = base / "brief.md"
        path.write_text(
            compose.load_document(brief, interest_name=interest_name).to_markdown(),
            encoding="utf-8",
        )
        written.append(path)

    for position, theme in enumerate(themes, 1):
        documents = _documents_by_type(deliverables_by_theme.get(theme["id"], []))
        if not any(documents.get(kind) for kind in ("summary", "script", "script_de")):
            continue
        ids = theme.get("source_article_ids") or []
        if isinstance(ids, str):
            ids = json.loads(ids)
        document = compose.theme_document(
            interest_name=interest_name,
            theme=theme,
            position=position,
            total=len(themes),
            summary=documents.get("summary"),
            script=documents.get("script"),
            script_de=documents.get("script_de"),
            sources=compose.resolve_sources(store, run_id, ids),
        )
        path = base / f"theme-{position:02d}-{slugify(theme['title'])}.md"
        path.write_text(document.to_markdown(), encoding="utf-8")
        written.append(path)

    logger.info(
        "Run output written — %d file(s) under %s (interest=%r, date=%s)",
        len(written), base, interest_name, run_date,
    )
    return written
