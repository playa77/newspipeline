"""HTML and plain-text renderers for report emails.

The HTML here is the primary artifact: a hand-designed, table-based report
layout for Gmail / Apple Mail / Outlook, with every style inline (the head
`<style>` block only carries the Apple-Mail-only dark-mode enhancement).
The plain-text part is rendered from the same `document.Document`, never by
stripping tags from the HTML.

Design constraints encoded below (see the `html-email-reports` skill and its
references/client-support.md for sources):

- nested `<table role="presentation">`, `width` attribute plus inline
  `max-width`; all spacing is padding on a `<td>` — Outlook desktop ignores
  `margin`, so nothing relies on it and every block gets its own row
- no flex/grid/position/float, no `background-image`, no `url(...)` — any
  `url()` makes Gmail drop the style attribute it appears in
- 600 px container, fluid to 320 px, body 17 px, ≥4.5:1 contrast on white
- dividers are 1 px `bgcolor` table rows, not styled `<hr>`
- every source list sits under the topic it belongs to, never pooled at the
  end of a multi-topic report
- the hidden preheader is the first child of `<body>`

Colors are deliberately not pure black/white: Gmail dark mode inverts
white↔black and recolors inline CSS, so every pair has to stay legible
inverted, and links always carry an inline color (uncolored links get
inverted into near-invisibility).
"""

# version: 0.1.0

from __future__ import annotations

import html
import textwrap

from newspipeline.document import Bullets, Document, Paragraph, Section, Source

# ---- design tokens -----------------------------------------------------------

PAGE = "#F7F7F5"
SURFACE = "#FFFFFF"
INK = "#1A1A1A"
SECONDARY = "#3F3F46"
MUTED = "#5B5B66"
LINK = "#1A56DB"
RULE = "#E4E4E7"

# Contrast on SURFACE (#FFFFFF): ink 17.4:1, secondary 10.4:1, muted 6.7:1,
# link 6.2:1 — all above the WCAG AA 4.5:1 floor for body text.
CONTRAST_PAIRS: tuple[tuple[str, str], ...] = (
    (INK, SURFACE), (SECONDARY, SURFACE), (MUTED, SURFACE), (LINK, SURFACE),
    (MUTED, PAGE),
)

FONT_SANS = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
)
FONT_SERIF = "Georgia,'Times New Roman',Times,serif"

# Apple-Mail-only dark-mode overrides (see _DARK_MODE_CSS) — deliberately not
# pure black/white, and listed here so the palette guard can see them.
DARK_PALETTE = ("#111214", "#1B1D21", "#EDEDED", "#B4B7BD")

CONTENT_WIDTH = 600
TEXT_WIDTH = 78
PREHEADER_MAX = 90

_DARK_MODE_CSS = f"""\
@media (prefers-color-scheme: dark) {{
  .np-bg {{ background:#111214 !important; }}
  .np-surface {{ background:#1B1D21 !important; }}
  .np-ink {{ color:#EDEDED !important; }}
  .np-muted {{ color:#B4B7BD !important; }}
}}\
"""


# ---- HTML -------------------------------------------------------------------


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _sans(size: int, line: int, color: str, *, weight: int = 400, extra: str = "") -> str:
    return (
        f"font-family:{FONT_SANS};font-size:{size}px;line-height:{line}px;"
        f"mso-line-height-rule:exactly;font-weight:{weight};color:{color};{extra}"
    )


def _row(padding: str, inner: str) -> str:
    return f'<tr><td style="padding:{padding};">{inner}</td></tr>'


def _rule() -> str:
    """A 1 px divider as a table row (styled <hr> is unreliable)."""
    return _row(
        "32px 24px 0",
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%"><tr>'
        f'<td bgcolor="{RULE}" height="1" style="height:1px;font-size:1px;'
        'line-height:1px;">&nbsp;</td></tr></table>',
    )


def _sources_row(section: Section, label: str) -> str:
    """A topic's sources, under that topic — never pooled at the report end."""
    if not section.sources:
        return ""
    items = []
    for source in section.sources:
        attribution = f" &mdash; {_esc(source.attribution)}" if source.attribution else ""
        items.append(
            f'<li style="margin:0;padding:0 0 4px;{_sans(15, 22, MUTED)}">'
            f'<a href="{_esc(source.url)}" '
            f'style="color:{LINK};text-decoration:underline;">{_esc(source.title)}</a>'
            f"{attribution}</li>"
        )
    return _row(
        "24px 24px 0",
        f'<div style="{_sans(15, 22, MUTED)}">'
        f'<strong style="color:{INK};">{_esc(label)}</strong>'
        '<ol style="margin:0;padding:0 0 0 20px;">' + "".join(items) + "</ol></div>",
    )


def _section_rows(section: Section, label: str, *, first: bool) -> list[str]:
    rows: list[str] = []
    if section.heading:
        rows.append(
            _row(
                ("0 24px 0" if first else "32px 24px 0"),
                f'<h2 class="np-ink" style="margin:0;{_sans(20, 26, INK, weight=700)}">'
                f"{_esc(section.heading)}</h2>",
            )
        )
    for block in section.blocks:
        if isinstance(block, Paragraph):
            rows.append(
                _row(
                    "16px 24px 0",
                    f'<p class="np-ink" style="margin:0;{_sans(17, 27, INK)}">'
                    f"{_esc(block.text)}</p>",
                )
            )
        else:
            items = "".join(
                f'<li style="margin:0;padding:0 0 8px;">{_esc(item)}</li>'
                for item in block.items
            )
            rows.append(
                _row(
                    "16px 24px 0",
                    f'<ul class="np-ink" style="margin:0;padding:0 0 0 20px;'
                    f'{_sans(17, 27, INK)}">{items}</ul>',
                )
            )
    sources = _sources_row(section, label)
    if sources:
        rows.append(sources)
    return rows


def _preheader(doc: Document) -> str:
    text = " ".join((doc.preheader or doc.deck or doc.headline).split())[:PREHEADER_MAX]
    return (
        f'<div style="display:none;font-size:1px;color:{PAGE};line-height:1px;'
        f'max-height:0;max-width:0;opacity:0;overflow:hidden;">{_esc(text)}</div>'
        # Spacer hack so the following body text cannot leak into the preview.
        '<div style="display:none;font-size:1px;line-height:1px;max-height:0;'
        'max-width:0;opacity:0;overflow:hidden;">'
        "&#847; &zwnj; &nbsp; &#8199; &shy; &#847; &zwnj; &nbsp; &#8199; &shy;</div>"
    )


def render_html(doc: Document) -> str:
    """Render a report document as the email's HTML part."""
    rows: list[str] = []
    if doc.kicker:
        rows.append(
            _row(
                "32px 24px 0",
                f'<div class="np-muted" style="{_sans(12, 16, MUTED, weight=700)}'
                'letter-spacing:1px;text-transform:uppercase;">'
                f"{_esc(doc.kicker)}</div>",
            )
        )
    if doc.headline:
        rows.append(
            _row(
                ("8px 24px 0" if doc.kicker else "32px 24px 0"),
                f'<h1 style="margin:0;font-family:{FONT_SERIF};font-size:28px;'
                f'line-height:34px;font-weight:700;color:{INK};">'
                f"{_esc(doc.headline)}</h1>",
            )
        )
    if doc.deck:
        rows.append(
            _row(
                "12px 24px 0",
                f'<div style="{_sans(19, 28, SECONDARY)}">{_esc(doc.deck)}</div>',
            )
        )
    if doc.sections:
        rows.append(_rule())
        for i, section in enumerate(doc.sections):
            rows.extend(_section_rows(section, doc.source_label, first=(i == 0)))
    for note in doc.notes:
        rows.append(_row("24px 24px 0", f'<div style="{_sans(15, 22, MUTED)}">{_esc(note)}</div>'))
    if doc.footer_note:
        rows.append(_rule())
        rows.append(
            _row(
                "16px 24px 32px",
                f'<div class="np-muted" style="{_sans(13, 20, MUTED)}">'
                f"{_esc(doc.footer_note)}</div>",
            )
        )

    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{_esc(doc.language)}">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>{_esc(doc.headline)}</title>\n"
        f"<style>{_DARK_MODE_CSS}</style>\n"
        "</head>\n"
        f'<body class="np-bg" style="margin:0;padding:0;background:{PAGE};'
        f'color:{INK};-webkit-text-size-adjust:100%;">\n'
        f"{_preheader(doc)}\n"
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="background:{PAGE};">\n'
        '<tr><td align="center" style="padding:24px 12px;">\n'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="{CONTENT_WIDTH}" class="np-surface" '
        f'style="width:100%;max-width:{CONTENT_WIDTH}px;background:{SURFACE};'
        f'border:1px solid {RULE};">\n'
        + "\n".join(rows)
        + "\n</table>\n</td></tr>\n</table>\n</body>\n</html>\n"
    )


# ---- plain text -------------------------------------------------------------


def _wrap(text: str, *, initial: str = "", subsequent: str = "") -> list[str]:
    """Wrap to the 78-char text-mail width, never breaking URLs."""
    return textwrap.wrap(
        " ".join(text.split()),
        width=TEXT_WIDTH,
        initial_indent=initial,
        subsequent_indent=subsequent,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [initial.rstrip()]


def _source_lines(source: Source) -> list[str]:
    """'- Title — url (Publisher, date)', splitting when the line gets long."""
    attribution = f" ({source.attribution})" if source.attribution else ""
    one_line = f"{source.title} \u2014 {source.url}{attribution}"
    if len(one_line) + 2 <= TEXT_WIDTH:  # +2 for the '- ' prefix
        return _wrap(one_line, initial="- ", subsequent="  ")
    # Too long for one line: keep the title on its own line and the URL with
    # its attribution on the next, rather than orphaning the attribution.
    return _wrap(source.title, initial="- ", subsequent="  ") + _wrap(
        f"{source.url}{attribution}", initial="  ", subsequent="  "
    )


def render_text(doc: Document) -> str:
    """Render the same document as the multipart/alternative text part."""
    lines: list[str] = []
    if doc.kicker:
        lines += [_wrap(doc.kicker.upper())[0], ""]
    lines += _wrap(doc.headline)
    if doc.deck:
        lines += [""] + _wrap(doc.deck)
    for i, section in enumerate(doc.sections):
        if i:
            lines += ["", "=" * TEXT_WIDTH, ""]
        else:
            lines += [""]
        if section.heading:
            lines += [section.heading.upper(), "-" * len(section.heading), ""]
        for block in section.blocks:
            if isinstance(block, Paragraph):
                lines += _wrap(block.text) + [""]
            else:
                for item in block.items:
                    lines += _wrap(item, initial="* ", subsequent="  ")
                lines += [""]
        if section.sources:
            lines += [f"{doc.source_label}:", ""]
            for source in section.sources:
                lines += _source_lines(source)
            lines += [""]
    if doc.notes:
        lines += ["-" * TEXT_WIDTH, ""]
        for note in doc.notes:
            lines += _wrap(note) + [""]
    if doc.footer_note:
        lines += ["-" * TEXT_WIDTH, ""] + _wrap(doc.footer_note)
    return "\n".join(lines).rstrip("\n") + "\n"
