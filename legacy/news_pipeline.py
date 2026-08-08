"""News Pipeline — core orchestration for RSS scraping, analysis, and generation.

Adapted from ai_news_scraper/src/pipeline.py.
Uses the shared OpenRouterClient and NewsStore for data persistence.
Supports: scrape, analyze, generate (summary + EN/DE scripts), brief, email.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from workbench.shared.llm.router import OpenRouterClient

logger = logging.getLogger(__name__)

# Common words for simple language detection heuristic
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
    """Detect whether text is German or English using word frequency heuristics.

    Counts occurrences of common German vs English words.
    If German words > English words * 1.5, returns "de", otherwise "en".
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


class NewsPipeline:
    def __init__(self, store: Any, session: Any):
        self._store = store
        self._session = session

    async def run(self, user_id: str, interest: dict, openrouter_key: str, base_url: str | None = None) -> int:
        interest_id = interest["id"]
        llm_kwargs: dict = {"api_key": openrouter_key, "rate_limit_user_id": user_id}
        if base_url:
            llm_kwargs["base_url"] = base_url
        llm = OpenRouterClient(**llm_kwargs)
        run_record = await self._store.create_run(interest_id)
        run_id = run_record["id"]

        try:
            await self._scrape(run_id, interest_id, interest)
            await self._analyze(run_id, interest, llm)
            await self._generate(run_id, interest, llm)
            await self._brief(run_id, interest, llm)
            await self._store.update_run(
                run_id, status="completed",
                    completed_at=datetime.now(tz=UTC).replace(tzinfo=None),
            )
            logger.info("Pipeline run %d completed successfully", run_id)
        except Exception as exc:
            logger.exception("Pipeline run %d failed", run_id)
            await self._store.update_run(run_id, status="failed", error=str(exc))
        finally:
            await llm.close()

        return run_id

    async def _scrape(self, run_id: int, interest_id: int, interest: dict) -> None:
        import feedparser
        import trafilatura

        await self._store.update_run(run_id, current_stage="scrape")
        feeds = await self._store.get_feeds_for_interest(interest_id)

        if not feeds:
            logger.info("No feeds configured for interest %d", interest_id)
            return

        timeout = interest.get("article_fetch_timeout_seconds", 30)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(float(timeout)),
            follow_redirects=True,
        ) as http:
            for feed in feeds:
                try:
                    parsed = feedparser.parse(feed["url"])
                    for entry in parsed.entries[:20]:
                        url = entry.get("link", "")
                        if not url:
                            continue
                        existing = await self._store.article_exists(url)
                        if existing:
                            continue

                        title = entry.get("title", "Untitled")
                        published = entry.get("published", datetime.now(UTC).isoformat())

                        content = None
                        content_status = "excerpt"
                        try:
                            resp = await http.get(url)
                            html = resp.text
                            content = trafilatura.extract(
                                html, include_comments=False, include_tables=False,
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

                        await self._store.insert_article(
                            run_id=run_id,
                            feed_id=feed["id"],
                            url=url,
                            title=title,
                            author=entry.get("author"),
                            published_at=published,
                            excerpt=entry.get("summary"),
                            content=content,
                            content_status=content_status,
                        )
                    logger.info("Scraped feed %s: %d entries", feed["name"], len(parsed.entries))
                except Exception as exc:
                    logger.warning("Failed to scrape feed %s: %s", feed["url"], exc)

    async def _analyze(self, run_id: int, interest: dict, llm: OpenRouterClient) -> None:
        await self._store.update_run(run_id, current_stage="analyze")
        articles = await self._store.get_articles_for_run(run_id)
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
                f"Title: {a['title']}\nSource: {a.get('url','')}\nBody: {body}"
            )

        prompt = (
            f"Analyze the following news articles. Identify {max_themes} major themes. "
            "For each theme, provide:\n"
            "1. A concise title (one line)\n"
            "2. A two-sentence description\n"
            "3. Which article numbers are relevant\n\n"
            "Format as JSON array:\n"
            '[{"title": "...", "description": "...", "article_indices": [1,3,5]}, ...]\n\n'
            "Articles:\n\n"
        )
        for i, text in enumerate(article_texts, 1):
            prompt += f"[{i}] {text}\n\n"

        model = interest.get("analysis_model")
        try:
            response = await llm.chat_completion(
                messages=[
                    {"role": "system", "content": "You are a news analyst. Respond only with valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                model=model,
                temperature=0.3,
                max_tokens=2000,
            )
            match = re.search(r"\[.*\]", response, re.DOTALL)
            if match:
                themes = json.loads(match.group(0))
                for i, theme in enumerate(themes[:max_themes]):
                    await self._store.insert_theme(
                        run_id=run_id,
                        title=theme.get("title", f"Theme {i+1}"),
                        description=theme.get("description", ""),
                        source_article_ids=theme.get("article_indices", []),
                        order_index=i,
                    )
                logger.info("Analyzed %d themes from %d articles", len(themes), len(article_texts))
        except Exception as exc:
            logger.error("Analysis failed: %s", exc)

    async def _generate(self, run_id: int, interest: dict, llm: OpenRouterClient) -> None:
        await self._store.update_run(run_id, current_stage="generate")
        themes = await self._store.get_themes_for_run(run_id)

        enable_summary = interest.get("enable_summary", True)
        enable_script = interest.get("enable_script", True)
        enable_script_de = interest.get("enable_script_de", False)
        model = interest.get("generation_model")

        for theme in themes:
            try:
                # Summary
                if enable_summary:
                    target_words = interest.get("target_summary_words", 750)
                    prompt = (
                        f"Write a {target_words}-word summary of this news theme:\n\n"
                        f"Theme: {theme['title']}\n"
                        f"Description: {theme['description']}\n\n"
                        "Write a well-structured, journalistic summary with a headline."
                    )
                    response = await llm.chat_completion(
                        messages=[
                            {"role": "system", "content": "You are a professional journalist. Write clear, engaging summaries."},
                            {"role": "user", "content": prompt},
                        ],
                        model=model,
                        temperature=0.5,
                        max_tokens=2000,
                    )
                    await self._store.insert_deliverable(theme["id"], "summary", response)

                # English script
                if enable_script:
                    target_script = interest.get("target_script_words", 1250)
                    script_prompt = (
                        f"Write a {target_script}-word YouTube script about this news theme:\n\n"
                        f"Theme: {theme['title']}\nContent: The theme is about {theme['description']}\n\n"
                        "Format as a video script with intro, segments, and outro. Include speaker directions in [brackets]."
                    )
                    script = await llm.chat_completion(
                        messages=[
                            {"role": "system", "content": "You are a video script writer. Write engaging YouTube scripts."},
                            {"role": "user", "content": script_prompt},
                        ],
                        model=model,
                        temperature=0.6,
                        max_tokens=3000,
                    )
                    await self._store.insert_deliverable(theme["id"], "script", script)

                # German script
                if enable_script_de:
                    target_script_de = interest.get("target_script_de_words", 1250)
                    de_prompt = (
                        f"Schreibe ein {target_script_de}-Wort YouTube-Skript auf Deutsch zu diesem Nachrichtenthema:\n\n"
                        f"Thema: {theme['title']}\nInhalt: {theme['description']}\n\n"
                        "Formatiere als Video-Skript mit Intro, Segmenten und Outro. "
                        "Füge Sprecheranweisungen in [Klammern] ein. Schreibe professionell und ansprechend."
                    )
                    script_de = await llm.chat_completion(
                        messages=[
                            {"role": "system", "content": "Du bist ein professioneller Drehbuchautor. Schreibe ansprechende YouTube-Skripte auf Deutsch."},
                            {"role": "user", "content": de_prompt},
                        ],
                        model=model,
                        temperature=0.6,
                        max_tokens=3000,
                    )
                    await self._store.insert_deliverable(theme["id"], "script_de", script_de)

                logger.info("Generated content for theme: %s", theme["title"])
            except Exception as exc:
                logger.error("Generation failed for theme %s: %s", theme["title"], exc)

    async def _brief(self, run_id: int, interest: dict, llm: OpenRouterClient) -> None:
        if not interest.get("enable_brief"):
            return
        await self._store.update_run(run_id, current_stage="brief")
        themes = await self._store.get_themes_for_run(run_id)
        if not themes:
            return

        theme_summaries = "\n\n".join(
            f"## {t['title']}\n{t['description']}" for t in themes
        )
        target_brief = interest.get("target_brief_words", 600)
        model = interest.get("brief_model")

        # Detect language from content
        language = detect_language(theme_summaries)

        try:
            if language == "de":
                prompt = (
                    "Fasse diese Nachrichtenthemen zu einer täglichen Kurznachricht zusammen:\n\n"
                    f"{theme_summaries}\n\n"
                    f"Schreibe einen zusammenhängenden {target_brief}-Wörter-Bericht, "
                    "der diese Themen verbindet und Zusammenhänge sowie wichtige Erkenntnisse hervorhebt. "
                    "Schreibe auf Deutsch. Verwende deutsche Überschriften."
                )
                system_content = "Du bist ein Redaktionsleiter. Schreibe prägnante, aufschlussreiche tägliche Nachrichten auf Deutsch."
            else:
                prompt = (
                    "Synthesize a daily news brief from these themes:\n\n"
                    f"{theme_summaries}\n\n"
                    f"Write a cohesive {target_brief}-word brief that ties these themes together, "
                    "highlighting connections and key takeaways."
                )
                system_content = "You are an executive editor. Write concise, insightful daily briefs."

            response = await llm.chat_completion(
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": prompt},
                ],
                model=model,
                temperature=0.4,
                max_tokens=1500,
            )
            await self._store.insert_brief(run_id, response)
            logger.info("Generated daily brief for run %d (language=%s)", run_id, language)
        except Exception as exc:
            logger.error("Brief generation failed: %s", exc)
