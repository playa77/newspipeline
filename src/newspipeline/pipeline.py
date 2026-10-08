"""News pipeline core: scrape → analyze → generate → brief.

Recipient-scoped: interests come from settings.toml, persistence via store.Store,
LLM via llm.LLMClient.

Stages:
1. scrape   — RSS feeds (feedparser) + article body extraction (trafilatura),
              URL dedupe across runs within the recipient/interest scope, configurable input-length modes
2. analyze  — LLM identifies major themes across the run's articles (JSON)
3. generate — per-theme deliverables: summary, EN script, optional DE script
4. brief    — daily brief synthesizing all themes, EN/DE auto-detected

Every generate/brief answer is stored as a report document (document.py,
built by compose.py): the HTML email, the text part and the markdown archive
are all renderings of that one structure, and no output format is derived
from another.

Delivery (email / local file) happens after the pipeline, driven by the
interest's `delivery` list — see emailer.py and fileout.py (WP5).
"""

# version: 0.7.0

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Iterator

import feedparser
import httpx
import trafilatura

from newspipeline.llm import LLMClient
from newspipeline.politeness import PolitenessLayer
from newspipeline import compose
from newspipeline import prompts as P
from newspipeline.document import Document
from newspipeline.store import Store, utcnow_iso

logger = logging.getLogger(__name__)

# Common words for the language heuristic.
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
    """Detect German vs English by word frequency.

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


def _extract_json_array(response: str) -> list[Any]:
    """Extract a JSON array of theme objects from an LLM response.

    Models wrap arrays in markdown fences, prose, or trailing commentary.
    Strategy: strip code fences, try the whole text, then scan every
    top-level balanced ``[...]`` region (string- and escape-aware; raw
    newlines terminate an open "string" since JSON forbids them). The first
    array containing at least one object wins; a literal ``[]`` anywhere is
    treated as a legitimate "no themes" answer and takes precedence over a
    dictless junk array. Raises ValueError when nothing usable is present —
    callers log and continue with partial results rather than crashing.
    """
    text = response.strip()
    # Strip a single markdown code fence pair if present.
    fence = re.match(r"^```[a-zA-Z0-9]*\s*\n?(.*?)\n?```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    def _balanced_regions(t: str) -> "Iterator[str]":
        """Yield every top-level ``[...]`` region that starts outside a
        string literal (string- and escape-aware)."""
        depth = 0
        in_string = False
        escaped = False
        start = -1
        for pos, ch in enumerate(t):
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                elif ch == "\n":
                    # A raw newline cannot occur inside a JSON string, so an
                    # open "string" here is really an unpaired quote in the
                    # surrounding prose — drop the state, not the rest of
                    # the input.
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "[":
                if depth == 0:
                    start = pos
                depth += 1
            elif ch == "]":
                if depth > 0:
                    depth -= 1
                    if depth == 0:
                        yield t[start:pos + 1]
                        start = -1

    candidates = [text, *_balanced_regions(text)]
    saw_empty_array = False
    junk_array: list[Any] | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, list):
            # e.g. the whole response is an object like {"themes": [...]}.
            # Not a win — but a bracket-scanned candidate may still follow.
            continue
        dicts = [item for item in parsed if isinstance(item, dict)]
        if dicts:
            return dicts
        # An array without theme objects: a literal [] is a legitimate
        # "no themes" answer; anything else ([1], ["x"]) is model junk.
        if not parsed:
            saw_empty_array = True
        elif junk_array is None:
            junk_array = parsed

    if saw_empty_array:
        return []
    if junk_array is not None:
        raise ValueError(
            f"theme array contained no objects: {junk_array!r:.200}"
        )
    raise ValueError("no JSON array found in model response")


def _document_json(document: Document) -> str:
    """Serialize a report document for storage (see document.DOCUMENT_FORMAT)."""
    return json.dumps(document.to_dict(), ensure_ascii=False)


def _article_ids(theme: dict[str, Any]) -> list[int]:
    """A theme's source article indices (stored as a JSON string in sqlite)."""
    ids = theme.get("source_article_ids") or []
    if isinstance(ids, str):
        ids = json.loads(ids)
    return ids


class Pipeline:
    """Runs one interest through scrape → analyze → generate → brief.

    Structure:
    - store is store.Store (sync sqlite3)
    - interest dict comes from settings.toml; feeds from interest["feeds"]
    - per-stage models: global.models.{analysis,generation,brief} with
      fallback to global.default_model
    - reasoning effort: global.reasoning.effort, forwarded per LLM call

    Stage failures (analysis, generation, brief) are logged and swallowed —
    a run completes with partial results rather than failing outright.
    Only unexpected errors outside the stage bodies fail the run.
    """

    def __init__(self, *, store: Store, settings: dict[str, Any], api_key: str) -> None:
        self._store = store
        self._settings = settings
        self._api_key = api_key
        global_cfg = settings.get("global", {})
        self._reasoning_effort: str | None = (
            (global_cfg.get("reasoning") or {}).get("effort") or None
        )

    def _stage_model(self, stage: str) -> str | None:
        """Resolve the model for a stage: per-stage override, else default."""
        global_cfg = self._settings.get("global", {})
        return (global_cfg.get("models") or {}).get(stage) or None

    @staticmethod
    def _detail_instruction(interest: dict[str, Any], kind: str) -> str:
        detail = interest.get(f"{kind}_detail", "standard")
        guidance = {
            "concise": "Prioritize essential facts; minimize background and examples.",
            "standard": "Balance key facts with useful context.",
            "detailed": "Include supporting context, implications, and concrete examples.",
        }[detail]
        return f"\nDetail level: {detail}. {guidance} Respect the requested word target."

    async def run(self, interest: dict[str, Any]) -> int:
        """Run the full pipeline for one interest. Returns the run id."""
        global_cfg = self._settings.get("global", {})
        llm = LLMClient(
            api_key=self._api_key,
            base_url=global_cfg.get("api_base_url", ""),
            default_model=global_cfg.get("default_model", ""),
        )
        run_record = self._store.create_run(interest["name"], interest.get("recipient_id", ""))
        run_id: int = run_record["id"]

        try:
            await self._scrape(run_id, interest)
            await self._analyze(run_id, interest, llm)
            await self._generate(run_id, interest, llm)
            await self._brief(run_id, interest, llm)
            self._store.update_run(run_id, status="completed", completed_at=utcnow_iso())
            logger.info("Pipeline run %d completed — interest=%r", run_id, interest["name"])
        except Exception as exc:
            logger.exception("Pipeline run %d failed — interest=%r", run_id, interest["name"])
            self._store.update_run(run_id, status="failed", error=str(exc))
        finally:
            await llm.close()

        return run_id

    async def _scrape(self, run_id: int, interest: dict[str, Any]) -> None:
        self._store.update_run(run_id, current_stage="scrape")
        feeds = interest.get("feeds", [])

        if not feeds:
            logger.info("No feeds configured for interest %r", interest.get("name"))
            return

        timeout = interest.get("article_fetch_timeout_seconds", 30)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(float(timeout)),
            follow_redirects=True,
        ) as http:
            # Politeness layer: shared minimum-gap schedule, bounded transient
            # retry, browser identity with single plain-identity fallback
            # (newspipeline/politeness.py). One layer per scrape run → the
            # spacing spans the feed fetch and every article fetch.
            polite = PolitenessLayer()
            for feed in feeds:
                try:
                    # The fetch goes through the timed httpx client below —
                    # feedparser's own URL fetcher has no timeout, and a
                    # stalled feed server must not wedge the run task (and
                    # with it the interest) or an executor thread past
                    # process shutdown. Only XML parsing is offloaded to a
                    # thread so the scheduler loop keeps ticking; feeds are
                    # still processed strictly sequentially.
                    resp = await polite.request(http, feed["url"])
                    parsed = await asyncio.to_thread(feedparser.parse, resp.content)
                    for entry in parsed.entries[:20]:
                        url = entry.get("link", "")
                        if not url:
                            continue
                        if self._store.article_exists(url, run_id=run_id):
                            continue

                        title = entry.get("title", "Untitled")
                        published = entry.get("published", "")

                        content = None
                        content_status = "excerpt"
                        try:
                            resp = await polite.request(http, url)
                            # trafilatura is CPU-bound parsing; keep it off
                            # the event loop for the same reason as above.
                            content = await asyncio.to_thread(
                                trafilatura.extract,
                                resp.text,
                                include_comments=False,
                                include_tables=False,
                            )
                            if content:
                                content_status = "full"
                        except Exception:
                            content = None

                        data_length_mode = interest.get("input_data_length_mode", "full_article")
                        if data_length_mode == "headers_only" and content:
                            content = None
                            content_status = "excerpt"
                        elif data_length_mode == "word_count" and content:
                            word_limit = interest.get("input_word_count", 256)
                            words = content.split()
                            if len(words) > word_limit:
                                content = " ".join(words[:word_limit])
                                content_status = "word_count"

                        self._store.insert_article(
                            run_id=run_id,
                            feed_name=feed["name"],
                            url=url,
                            title=title,
                            author=entry.get("author"),
                            published_at=published,
                            excerpt=entry.get("summary"),
                            content=content,
                            content_status=content_status,
                        )
                    logger.info(
                        "Scraped feed %r: %d entries — interest=%r",
                        feed["name"], len(parsed.entries), interest.get("name"),
                    )
                except Exception as exc:
                    logger.warning("Failed to scrape feed %s: %s", feed["url"], exc)

    async def _analyze(self, run_id: int, interest: dict[str, Any], llm: LLMClient) -> None:
        self._store.update_run(run_id, current_stage="analyze")
        articles = self._store.get_articles_for_run(run_id)
        if not articles:
            logger.info("No articles to analyze for run %d", run_id)
            return

        max_arts = interest.get("max_themes_source_articles", 30)
        max_themes = interest.get("max_themes", 5)
        article_texts: list[str] = []
        for a in articles[:max_arts]:
            body = a.get("content") or a.get("excerpt") or ""
            if len(body) > 2000:
                body = body[:2000]
            article_texts.append(
                f"Title: {a['title']}\nSource: {a.get('url', '')}\nBody: {body}"
            )

        system = P.ANALYZE_SYSTEM + "\nGroup related coverage only among the supplied articles for this interest. Do not import other interests or sources."
        prompt = (
            P.ANALYZE_USER.format(
                max_themes=max_themes,
                numbered_articles="\n\n".join(
                    f"[{i}] {text}" for i, text in enumerate(article_texts, 1)
                ),
            )
        )

        response: str | None = None
        try:
            response = await llm.chat_completion(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                model=self._stage_model("analysis"),
                temperature=0.3,
                # Reasoning models spend from this budget on thinking too —
                # a 2k cap starves glm-5.3-flash at default effort "max"
                # into returning content=null (G10's trigger).
                max_tokens=32000,
                reasoning_effort=self._reasoning_effort,
            )
            themes = _extract_json_array(response)
            for i, theme in enumerate(themes[:max_themes]):
                self._store.insert_theme(
                    run_id=run_id,
                    title=theme.get("title", f"Theme {i+1}"),
                    description=theme.get("description", ""),
                    source_article_ids=theme.get("article_indices", []),
                    order_index=i,
                )
            logger.info(
                "Analyzed %d themes from %d articles — run=%d",
                len(themes), len(article_texts), run_id,
            )
        except Exception as exc:
            # Include the offending response (truncated) so a parse failure
            # can be diagnosed from the log alone.
            logger.error(
                "Analysis failed for run %d: %s — response=%s",
                run_id, exc, (response or "")[:500],
            )

    async def _generate(self, run_id: int, interest: dict[str, Any], llm: LLMClient) -> None:
        self._store.update_run(run_id, current_stage="generate")
        themes = self._store.get_themes_for_run(run_id)

        enable_summary = interest.get("enable_summary", True)
        enable_script = interest.get("enable_script", True)
        enable_script_de = interest.get("enable_script_de", False)
        model = self._stage_model("generation")

        for theme in themes:
            try:
                if enable_summary:
                    target_words = interest.get("target_summary_words", 600)
                    prompt = (
                        P.SUMMARY_USER.format(
                            target_words=target_words,
                            theme_title=theme['title'],
                            theme_description=theme['description'],
                        )
                    )
                    prompt += self._detail_instruction(interest, "summary")
                    response = await llm.chat_completion(
                        messages=[
                            {"role": "system", "content": P.SUMMARY_SYSTEM},
                            {"role": "user", "content": prompt},
                        ],
                        model=model,
                        temperature=0.5,
                        # 32k: reasoning tokens share this budget (G10).
                        max_tokens=32000,
                        reasoning_effort=self._reasoning_effort,
                    )
                    document = compose.summary_document(
                        payload=compose.extract_json_object(response),
                        raw=response,
                        theme_title=theme["title"],
                        theme_description=theme["description"],
                    )
                    self._store.insert_deliverable(
                        theme_id=theme["id"], deliverable_type="summary",
                        content=_document_json(document),
                    )

                if enable_script:
                    target_script = interest.get("target_script_words", 1250)
                    script_prompt = (
                        P.SCRIPT_EN_USER.format(
                            target_words=target_script,
                            theme_title=theme['title'],
                            theme_description=theme['description'],
                        )
                    )
                    script_prompt += self._detail_instruction(interest, "script")
                    script = await llm.chat_completion(
                        messages=[
                            {"role": "system", "content": P.SCRIPT_EN_SYSTEM},
                            {"role": "user", "content": script_prompt},
                        ],
                        model=model,
                        temperature=0.6,
                        # 32k: reasoning tokens share this budget (G10).
                        max_tokens=32000,
                        reasoning_effort=self._reasoning_effort,
                    )
                    self._store.insert_deliverable(
                        theme_id=theme["id"], deliverable_type="script",
                        content=_document_json(
                            compose.script_document(
                                payload=compose.extract_json_object(script), raw=script
                            )
                        ),
                    )

                if enable_script_de:
                    target_script_de = interest.get("target_script_de_words", 1250)
                    de_prompt = (
                        P.SCRIPT_DE_USER.format(
                            target_words=target_script_de,
                            theme_title=theme['title'],
                            theme_description=theme['description'],
                        )
                    )
                    de_prompt += self._detail_instruction(interest, "script_de")
                    script_de = await llm.chat_completion(
                        messages=[
                            {"role": "system", "content": P.SCRIPT_DE_SYSTEM},
                            {"role": "user", "content": de_prompt},
                        ],
                        model=model,
                        temperature=0.6,
                        # 32k: reasoning tokens share this budget (G10).
                        max_tokens=32000,
                        reasoning_effort=self._reasoning_effort,
                    )
                    self._store.insert_deliverable(
                        theme_id=theme["id"], deliverable_type="script_de",
                        content=_document_json(
                            compose.script_document(
                                payload=compose.extract_json_object(script_de),
                                raw=script_de,
                            )
                        ),
                    )

                logger.info("Generated content for theme: %s", theme["title"])
            except Exception as exc:
                logger.error("Generation failed for theme %s: %s", theme["title"], exc)

    async def _brief(self, run_id: int, interest: dict[str, Any], llm: LLMClient) -> None:
        if not interest.get("enable_brief"):
            return
        self._store.update_run(run_id, current_stage="brief")
        themes = self._store.get_themes_for_run(run_id)
        if not themes:
            return

        # Themes are numbered: the brief prompt promises numbers, and the
        # model echoes the number back per section ("theme_number"), which is
        # what binds a topic's source links to that topic (compose.brief_document).
        theme_summaries = "\n\n".join(
            f"## {n}. {t['title']}\n{t['description']}" for n, t in enumerate(themes, 1)
        )
        target_brief = interest.get("target_brief_words", 690)

        language = detect_language(theme_summaries)

        try:
            if language == "de":
                prompt = P.BRIEF_DE_USER.format(
                    target_words=target_brief,
                    theme_summaries=theme_summaries,
                )
                system_content = P.BRIEF_DE_SYSTEM
            else:
                prompt = P.BRIEF_EN_USER.format(
                    target_words=target_brief,
                    theme_summaries=theme_summaries,
                )
                system_content = P.BRIEF_EN_SYSTEM

            prompt += self._detail_instruction(interest, "brief")
            response = await llm.chat_completion(
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": prompt},
                ],
                model=self._stage_model("brief"),
                temperature=0.4,
                # 32k: reasoning tokens share this budget (G10).
                max_tokens=32000,
                reasoning_effort=self._reasoning_effort,
            )
            # The brief document carries each topic's own sources: a section
            # the model tied to theme n gets that theme's articles, and any
            # theme it left out gets a heading-only section with its own links
            # (compose.brief_document). Nothing is pooled and nothing is lost.
            sources_by_theme = {
                n: compose.resolve_sources(self._store, run_id, _article_ids(theme))
                for n, theme in enumerate(themes, 1)
            }
            document = compose.brief_document(
                payload=compose.extract_json_object(response),
                raw=response,
                themes=themes,
                sources_by_theme=sources_by_theme,
                interest_name=interest.get("name", "Interest"),
                run_date=(self._store.get_run(run_id) or {}).get("run_date", ""),
            )
            self._store.insert_brief(
                run_id=run_id,
                content=_document_json(document),
                word_count=document.word_count(),
            )
            logger.info("Generated daily brief for run %d (language=%s)", run_id, language)
        except Exception as exc:
            logger.error("Brief generation failed for run %d: %s", run_id, exc)
