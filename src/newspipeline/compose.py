"""Stage output → report documents.

The generate and brief stages answer with a JSON object describing the
article (schema per stage, see prompts.py); this module turns that answer
into the `document.Document` the renderers consume, resolves the source
articles into the section they belong to, and assembles the per-theme report
from its stored deliverables.

Two rules shape the code here:

- **The HTML is not derived from text.** A stage answer either yields
  structure (headline, deck, paragraphs, sections) or it yields a fallback
  document whose prose is placed in the designed layout unchanged — the
  renderer never re-parses anything.
- **A run never fails on format.** A model that answers with prose, or with
  JSON missing half its keys, produces a degraded-but-rendered document and
  a log line; only a missing/empty answer is an error (and that is already
  G10's territory).

Source attribution lives here too: a brief section carries the sources of the
theme it reports on, so no link can be attributed to the wrong topic, and
the theme report carries the theme's own articles.
"""

# version: 0.1.0

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

from newspipeline.document import (
    Bullets,
    Document,
    Paragraph,
    Section,
    Source,
)

logger = logging.getLogger(__name__)

FALLBACK_NOTE = (
    "The model answered without the expected structure; the text below is "
    "shown as delivered."
)


# ---- JSON extraction ---------------------------------------------------------


def _strip_fence(text: str) -> str:
    fence = re.match(r"^```[a-zA-Z0-9]*\s*\n?(.*?)\n?```\s*$", text.strip(), re.DOTALL)
    return fence.group(1).strip() if fence else text.strip()


def _balanced_objects(text: str) -> Iterator[str]:
    """Yield every top-level ``{...}`` region (string- and escape-aware)."""
    depth = 0
    in_string = False
    escaped = False
    start = -1
    for pos, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            elif char == "\n":  # raw newlines cannot occur inside a JSON string
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = pos
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0:
                    yield text[start:pos + 1]
                    start = -1


def extract_json_object(response: str) -> dict[str, Any] | None:
    """Parse a stage answer into a dict, or None if it holds no JSON object.

    Models wrap JSON in code fences or prose; the whole answer is tried
    first, then every balanced ``{...}`` region. Only dicts count.
    """
    text = _strip_fence(response or "")
    for candidate in (text, *_balanced_objects(text)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


# ---- small coercions ---------------------------------------------------------


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [item for item in (_text(v) for v in value) if item]
    return []


def _prose_paragraphs(raw: str) -> list[str]:
    """Degradation path: the paragraph blocks of a non-JSON answer."""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", (raw or "").strip())]
    return [block for block in blocks if block] or ([raw.strip()] if raw.strip() else [])


def _paragraph_blocks(texts: list[str]) -> tuple[Paragraph, ...]:
    return tuple(Paragraph(text) for text in texts)


# ---- sources -----------------------------------------------------------------


def resolve_sources(store: Any, run_id: int, article_ids: list[int]) -> tuple[Source, ...]:
    """Resolve 1-based article indices to sources, skipping unresolvable ones."""
    sources: list[Source] = []
    seen: set[str] = set()
    for article in store.get_articles_by_ids(run_id, list(article_ids)):
        url = _text(article.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append(
            Source(
                title=_text(article.get("title")) or url,
                url=url,
                publisher=_text(article.get("feed_name")),
                date=_text(article.get("published_at"))[:10],
            )
        )
    return tuple(sources)


# ---- per-stage documents -----------------------------------------------------


def summary_document(
    *, payload: dict[str, Any] | None, raw: str, theme_title: str,
    theme_description: str,
) -> Document:
    """The per-topic article: headline, deck, body paragraphs."""
    payload = payload or {}
    headline = _text(payload.get("headline")) or theme_title
    deck = _text(payload.get("deck"))
    paragraphs = _text_list(payload.get("paragraphs"))
    notes: tuple[str, ...] = ()
    if not paragraphs:
        paragraphs = _prose_paragraphs(raw)
        notes = (FALLBACK_NOTE,)
        logger.warning("Summary answered without paragraphs — using the prose fallback")
    if not deck:
        deck = theme_description
    return Document(
        headline=headline,
        deck=deck,
        sections=(Section(blocks=_paragraph_blocks(paragraphs)),),
        notes=notes,
    )


def script_document(*, payload: dict[str, Any] | None, raw: str) -> Document:
    """A video script: one section per part, each heading a speaker direction."""
    payload = payload or {}
    sections: list[Section] = []
    for item in payload.get("sections") or []:
        if not isinstance(item, dict):
            continue
        paragraphs = _text_list(item.get("paragraphs")) or _text_list(item.get("text"))
        if not paragraphs:
            continue
        sections.append(
            Section(
                heading=_text(item.get("heading")),
                blocks=_paragraph_blocks(paragraphs),
            )
        )
    notes: tuple[str, ...] = ()
    if not sections:
        sections = [Section(blocks=_paragraph_blocks(_prose_paragraphs(raw)))]
        notes = (FALLBACK_NOTE,)
        logger.warning("Script answered without sections — using the prose fallback")
    return Document(sections=tuple(sections), notes=notes)


def brief_document(
    *,
    payload: dict[str, Any] | None,
    raw: str,
    themes: list[dict[str, Any]],
    sources_by_theme: dict[int, tuple[Source, ...]],
    interest_name: str,
    run_date: str = "",
) -> Document:
    """The daily brief: one section per topic, each carrying its own sources.

    A section the model tied to theme *n* gets that theme's articles, so the
    reader can always tell which link belongs to which topic. Themes the model
    left out are appended as heading-only sections with their sources, so a
    link is never lost and never attached to the wrong topic.
    """
    payload = payload or {}
    headline = _text(payload.get("headline"))
    deck = _text(payload.get("deck"))
    sections: list[Section] = []
    referenced: set[int] = set()

    for item in payload.get("sections") or []:
        if not isinstance(item, dict):
            continue
        number = _theme_number(item.get("theme_number"), len(themes))
        heading = _text(item.get("heading"))
        paragraphs = _text_list(item.get("paragraphs")) or _text_list(item.get("text"))
        if number is not None:
            referenced.add(number)
            topic = _text(themes[number - 1].get("title"))
            heading = heading or topic
            sources = sources_by_theme.get(number, ())
        else:
            topic, sources = "", ()
        if not paragraphs and not sources:
            continue
        sections.append(
            Section(
                heading=heading,
                blocks=_paragraph_blocks(paragraphs),
                sources=sources,
                topic=topic,
            )
        )

    watch = _text_list(payload.get("what_to_watch") or payload.get("watch"))
    if watch:
        sections.append(Section(heading="What to watch", blocks=(Bullets(tuple(watch)),)))

    # Whatever came out of the payload: if this is still empty, the model
    # answered in prose and the report needs its text plus the fallback note.
    structured = bool(sections)

    for number, theme in enumerate(themes, 1):
        sources = sources_by_theme.get(number, ())
        if number in referenced or not sources:
            continue
        sections.append(
            Section(
                heading=_text(theme.get("title")),
                sources=sources,
                topic=_text(theme.get("title")),
            )
        )

    notes: tuple[str, ...] = ()
    if not structured:
        # The prose goes in first, the per-theme source blocks stay after it,
        # so a formatting failure costs the reader structure, never a link.
        sections.insert(0, Section(blocks=_paragraph_blocks(_prose_paragraphs(raw))))
        notes = (FALLBACK_NOTE,)
        logger.warning("Brief answered without sections — using the prose fallback")
        headline = headline or _text(raw.split("\n", 1)[0])

    kicker = f"{interest_name} · Daily Brief"
    if run_date:
        kicker = f"{kicker} · {run_date}"
    return Document(
        kicker=kicker,
        headline=headline,
        deck=deck,
        sections=tuple(sections),
        notes=notes,
    )


def _theme_number(value: Any, theme_count: int) -> int | None:
    """The 1-based theme number a section names, or None when unusable."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= theme_count else None


def theme_document(
    *,
    interest_name: str,
    theme: dict[str, Any],
    position: int,
    total: int,
    summary: Document | None = None,
    script: Document | None = None,
    script_de: Document | None = None,
    sources: tuple[Source, ...] = (),
) -> Document:
    """Assemble one theme's report from its stored deliverables.

    One deliverable renders as itself; several get a section heading each so
    the reader can tell a summary from a script. The theme's sources sit at
    the end of the report — a single-topic report has one source list.
    """
    parts: list[tuple[str, Document]] = []
    if summary is not None:
        parts.append(("Summary", summary))
    if script is not None:
        parts.append(("Script (English)", script))
    if script_de is not None:
        parts.append(("Script (German)", script_de))

    sections: list[Section] = []
    if len(parts) == 1:
        sections = list(parts[0][1].sections)
    else:
        for label, document in parts:
            if len(document.sections) == 1 and not document.sections[0].heading:
                # A single un-headed section (a summary) takes the label as
                # its heading instead of gaining an extra empty section.
                only = document.sections[0]
                sections.append(
                    Section(
                        heading=label,
                        blocks=only.blocks,
                        sources=only.sources,
                        topic=only.topic,
                    )
                )
            else:
                sections.append(Section(heading=label))
                sections.extend(document.sections)

    if sources and sections:
        last = sections[-1]
        sections[-1] = Section(
            heading=last.heading, blocks=last.blocks, sources=sources, topic=last.topic
        )
    elif sources:
        sections = [Section(sources=sources)]

    headline = (summary.headline if summary and summary.headline else "") or _text(
        theme.get("title")
    )
    deck = (summary.deck if summary else "") or _text(theme.get("description"))
    return Document(
        kicker=f"{interest_name} · theme {position} of {total}",
        headline=headline,
        deck=deck,
        sections=tuple(sections),
        footer_note=footer_note(interest_name),
    )


def footer_note(interest_name: str) -> str:
    """The provenance line every report ends with (why this mail arrived)."""
    return (
        f"Sent by newspipeline for the interest '{interest_name}'. Delivery and "
        "schedule are configured in the desktop client."
    )


# ---- reading stored documents ------------------------------------------------


def load_document(content: str, *, interest_name: str = "") -> Document:
    """Parse a stored document JSON; degrade to prose on anything unexpected."""
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict) and "sections" in data:
        document = Document.from_dict(data)
        if interest_name and not document.footer_note:
            document = Document.from_dict({**data, "footer_note": footer_note(interest_name)})
        return document
    logger.warning("Stored content is not a document object — rendering it as prose")
    return Document(
        headline="",
        sections=(Section(blocks=_paragraph_blocks(_prose_paragraphs(content or ""))),),
        notes=(FALLBACK_NOTE,),
    )
