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

    TODO(WP5): implement.
    """
    raise NotImplementedError("WP5")
