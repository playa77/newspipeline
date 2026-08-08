"""Local-file delivery mode: write pipeline output as markdown files.

New in the standalone app (the legacy monolith delivered only via email).
Output layout:

    <output_dir>/<interest-slug>/<run-date>/
        brief.md
        theme-01-<slug>.md   (summary / scripts, one file per theme)
"""

# version: 0.1.0

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def slugify(text: str) -> str:
    """Filesystem-safe slug: lowercase, alphanumerics and dashes only."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "untitled"


# Display order and headings for deliverable types inside a theme file.
_DELIVERABLE_SECTIONS = (
    ("summary", "Summary"),
    ("script", "Script (EN)"),
    ("script_de", "Script (DE)"),
)


def write_run_output(
    *,
    output_dir: Path | str,
    interest_name: str,
    run_date: str,
    brief: str | None,
    themes: list[dict[str, Any]],
    deliverables_by_theme: dict[int, list[dict[str, Any]]],
) -> list[Path]:
    """Write one run's brief and theme deliverables to disk.

    Returns the list of written file paths.
    """
    base = Path(output_dir) / slugify(interest_name) / run_date
    base.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []

    if brief:
        path = base / "brief.md"
        path.write_text(
            f"# Daily Brief — {interest_name} — {run_date}\n\n{brief}\n",
            encoding="utf-8",
        )
        written.append(path)

    for i, theme in enumerate(themes, 1):
        by_type = {
            d.get("deliverable_type"): d.get("content", "")
            for d in deliverables_by_theme.get(theme["id"], [])
        }
        sections = [
            (heading, by_type[dtype])
            for dtype, heading in _DELIVERABLE_SECTIONS
            if by_type.get(dtype)
        ]
        if not sections:
            continue
        parts = [f"# {theme['title']}"]
        if theme.get("description"):
            parts += ["", theme["description"]]
        for heading, content in sections:
            parts += ["", f"## {heading}", "", content]
        path = base / f"theme-{i:02d}-{slugify(theme['title'])}.md"
        path.write_text("\n".join(parts) + "\n", encoding="utf-8")
        written.append(path)

    logger.info(
        "Run output written — %d file(s) under %s (interest=%r, date=%s)",
        len(written), base, interest_name, run_date,
    )
    return written
