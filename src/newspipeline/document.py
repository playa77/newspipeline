"""The document model every output format is rendered from.

The HTML email is a first-class artifact, not a rendering of markdown: the
email's layout is designed as HTML (see email_layout.py), and the markdown
archive and the plain-text mail part are siblings rendered from the same
structure. Nothing in the render path parses text back into meaning.

A report document is:

    Document(kicker, headline, deck, sections[Section], footer_note, ...)
    Section(heading, blocks[Paragraph | Bullets], sources[Source], topic)

``Section.topic`` names the topic the section reports on (empty for a
single-topic report). It is what binds a section's sources to it — the
reader can always tell which link belongs to which topic, which is exactly
what a single pooled list at the end of a multi-topic brief cannot do.

Model rules:
- Blocks are explicit: a paragraph is a paragraph, a bullet list is a list.
  Nothing downstream guesses structure from punctuation or line breaks.
- Sources carry the article's own title and URL, optionally publisher/date.
- Fields are plain strings and frozen tuples, so a document can be built
  from dicts (as delivered by the LLM) and compared in tests.
"""

# version: 0.2.0

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Union

# Marker stored with every persisted document (see Document.to_dict), so a
# future format change is detectable instead of silent. There is no
# compatibility layer for other values: this is a fresh install and the
# stored format is whatever this constant says.
DOCUMENT_FORMAT = 1


@dataclass(frozen=True)
class Source:
    """One cited article."""

    title: str
    url: str
    publisher: str = ""
    date: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Source":
        return cls(
            title=str(data.get("title") or data.get("url") or "").strip(),
            url=str(data.get("url") or "").strip(),
            publisher=str(data.get("publisher") or "").strip(),
            date=str(data.get("date") or "").strip(),
        )

    @property
    def attribution(self) -> str:
        """'Publisher, date' — whichever parts exist, else empty."""
        return ", ".join(part for part in (self.publisher, self.date) if part)


@dataclass(frozen=True)
class Paragraph:
    """A body paragraph."""

    text: str


@dataclass(frozen=True)
class Bullets:
    """A bullet list."""

    items: tuple[str, ...]

    @classmethod
    def from_iterable(cls, items: Iterable[Any]) -> "Bullets":
        return cls(tuple(str(item).strip() for item in items if str(item).strip()))


Block = Union[Paragraph, Bullets]


@dataclass(frozen=True)
class Section:
    """One topic of a report: a heading, its blocks, and its own sources."""

    heading: str = ""
    blocks: tuple[Block, ...] = ()
    sources: tuple[Source, ...] = ()
    topic: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Section":
        blocks: list[Block] = []
        for para in data.get("paragraphs") or []:
            if str(para).strip():
                blocks.append(Paragraph(str(para).strip()))
        bullets = [str(item).strip() for item in data.get("bullets") or []]
        if bullets:
            blocks.append(Bullets(tuple(item for item in bullets if item)))
        return cls(
            heading=str(data.get("heading") or "").strip(),
            blocks=tuple(blocks),
            sources=tuple(
                Source.from_dict(item) for item in data.get("sources") or []
            ),
            topic=str(data.get("topic") or "").strip(),
        )


@dataclass(frozen=True)
class Document:
    """A complete report: the one structure all outputs are rendered from."""

    headline: str = ""
    kicker: str = ""
    deck: str = ""
    sections: tuple[Section, ...] = ()
    footer_note: str = ""
    preheader: str = ""
    language: str = "en"
    subject: str = ""
    source_label: str = "Sources"
    # Free-form trailing notes (e.g. a run's metadata) — rendered last.
    notes: tuple[str, ...] = field(default=())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Document":
        return cls(
            headline=str(data.get("headline") or "").strip(),
            kicker=str(data.get("kicker") or "").strip(),
            deck=str(data.get("deck") or "").strip(),
            sections=tuple(Section.from_dict(s) for s in data.get("sections") or []),
            footer_note=str(data.get("footer_note") or "").strip(),
            preheader=str(data.get("preheader") or "").strip(),
            language=str(data.get("language") or "en").strip() or "en",
            subject=str(data.get("subject") or "").strip(),
            source_label=str(data.get("source_label") or "Sources").strip(),
            notes=tuple(str(n).strip() for n in data.get("notes") or [] if str(n).strip()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": DOCUMENT_FORMAT,
            "headline": self.headline,
            "kicker": self.kicker,
            "deck": self.deck,
            "sections": [
                {
                    "heading": section.heading,
                    "topic": section.topic,
                    "paragraphs": [
                        block.text for block in section.blocks if isinstance(block, Paragraph)
                    ],
                    "bullets": [
                        item for block in section.blocks if isinstance(block, Bullets)
                        for item in block.items
                    ],
                    "sources": [
                        {
                            "title": source.title,
                            "url": source.url,
                            "publisher": source.publisher,
                            "date": source.date,
                        }
                        for source in section.sources
                    ],
                }
                for section in self.sections
            ],
            "footer_note": self.footer_note,
            "preheader": self.preheader,
            "language": self.language,
            "subject": self.subject,
            "source_label": self.source_label,
            "notes": list(self.notes),
        }

    def word_count(self) -> int:
        """Words of prose in the document (headline, deck, blocks, notes)."""
        parts = [self.headline, self.deck]
        for section in self.sections:
            parts.append(section.heading)
            for block in section.blocks:
                parts.extend(block.items if isinstance(block, Bullets) else [block.text])
        parts.extend(self.notes)
        return len(" ".join(parts).split())

    def to_markdown(self) -> str:
        """The archive rendering: markdown, derived FROM this document.

        This is a sibling output, not the source of the email. Links are
        markdown links so the archive stays clickable.
        """
        lines: list[str] = []
        if self.kicker:
            lines += [f"*{self.kicker}*", ""]
        lines += [f"# {self.headline}", ""]
        if self.deck:
            lines += [self.deck, ""]
        for section in self.sections:
            if section.heading:
                lines += [f"## {section.heading}", ""]
            for block in section.blocks:
                if isinstance(block, Paragraph):
                    lines += [block.text, ""]
                else:
                    lines += [f"- {item}" for item in block.items] + [""]
            if section.sources:
                lines += [f"**{self.source_label}**", ""]
                for i, source in enumerate(section.sources, 1):
                    title = source.title.replace("[", "(").replace("]", ")")
                    suffix = f" — {source.attribution}" if source.attribution else ""
                    lines.append(f"{i}. [{title}]({source.url}){suffix}")
                lines.append("")
        for note in self.notes:
            lines += [note, ""]
        if self.footer_note:
            lines += ["---", "", self.footer_note, ""]
        return "\n".join(lines).rstrip("\n") + "\n"
