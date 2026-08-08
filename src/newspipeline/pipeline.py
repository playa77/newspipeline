"""News pipeline core: scrape → analyze → generate → brief.

Ported stage-for-stage from legacy/news_pipeline.py. Single-user: interests
come from settings.toml, persistence via store.Store, LLM via llm.LLMClient.

Stages:
1. scrape   — RSS feeds (feedparser) + article body extraction (trafilatura),
              URL dedupe across all runs, configurable input-length modes
2. analyze  — LLM identifies major themes across the run's articles (JSON)
3. generate — per-theme deliverables: summary, EN script, optional DE script
4. brief    — daily brief synthesizing all themes, EN/DE auto-detected

Delivery (email / local file) happens after the pipeline, driven by the
interest's `delivery` list — see emailer.py and fileout.py (WP5).
"""

# version: 0.1.0

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Common words for the language heuristic (ported from legacy).
_GERMAN_WORDS: set[str] = {
    "der", "die", "das", "und", "ist", "sind", "ein", "eine", "auf", "für",
    "mit", "von", "zu", "im", "den", "dem", "des", "sich", "nicht", "auch",
    "werden", "hat", "bei", "nach", "aus", "über", "zum", "zur", "unter",
    "vor", "zwischen", "durch", "gegen", "ohne", "um", "bis", "seit", "ab",
    "an", "dass", "wenn", "aber", "oder", "weil",
}

_ENGLISH_WORDS: set[str] = {
    "the", "a", "an", "and", "is", "are", "was", "were", "for", "with",
    "from", "to", "in", "on", "at", "by", "of", "that", "this", "it",
    "not", "also", "will", "has", "have", "but", "or", "because",
}


def detect_language(text: str) -> str:
    """Detect German vs English by word frequency (ported from legacy).

    Returns "de" when German hits exceed English hits by 1.5x, else "en".
    Falls back to "en" on any error.
    """
    if not text:
        return "en"
    try:
        words = text.lower().split()
        if not words:
            return "en"
        de_count = sum(1 for w in words if w in _GERMAN_WORDS)
        en_count = sum(1 for w in words if w in _ENGLISH_WORDS)
        return "de" if de_count > en_count * 1.5 else "en"
    except Exception:
        return "en"


class Pipeline:
    """Runs one interest through scrape → analyze → generate → brief.

    TODO(WP4): implement run() and the four stage methods, ported from
    legacy NewsPipeline with these adaptations:
    - store is store.Store (sync sqlite3) instead of NewsStore (async pg)
    - interest dict comes from settings.toml; feeds from interest["feeds"]
    - per-stage models: global.models.{analysis,generation,brief} with
      fallback to global.default_model
    - reasoning effort: global.reasoning.effort, forwarded per LLM call
    """

    def __init__(self, *, store: Any, settings: dict[str, Any], api_key: str) -> None:
        self._store = store
        self._settings = settings
        self._api_key = api_key

    async def run(self, interest: dict[str, Any]) -> int:
        """Run the full pipeline for one interest. Returns the run id."""
        raise NotImplementedError("WP4")
