"""Guard tests for the HTML-first email layout (email_layout.py).

The HTML email is a first-class artifact, so these assertions are about the
HTML itself — its structure, its inline styling, and what it must never
contain — rather than about the text it was built from. The rules encoded
here are the ones that decide whether the mail survives Gmail, Apple Mail
and Outlook desktop; sources for each are in the `html-email-reports`
skill's references/client-support.md.
"""

# version: 0.1.0

import re

import pytest
from lxml import html as lxml_html

from newspipeline.document import Bullets, Document, Paragraph, Section, Source
from newspipeline.email_layout import (
    CONTRAST_PAIRS,
    CONTENT_WIDTH,
    DARK_PALETTE,
    LINK,
    PREHEADER_MAX,
    RULE,
    TEXT_WIDTH,
    render_html,
    render_text,
)

# ---- fixtures ----------------------------------------------------------------

TOPIC_ONE = Section(
    heading="EU tightens AI rules",
    blocks=(
        Paragraph(
            "Member states approved the final text of the AI act's implementing "
            "rules on Tuesday, clearing the last hurdle before enforcement "
            "begins in March."
        ),
        Bullets(
            (
                "High-risk systems must register with national supervisors",
                "Penalties reach 3% of global turnover",
            )
        ),
    ),
    sources=(
        Source("AI act clears final hurdle", "https://example.test/ai-1",
               publisher="Example Wire", date="2026-09-16"),
        Source("Supervisors brace for registrations", "https://example.test/ai-2",
               publisher="Example Wire", date="2026-09-15"),
    ),
    topic="EU tightens AI rules",
)

TOPIC_TWO = Section(
    heading="Chip export rules widen",
    blocks=(
        Paragraph(
            "The export list gained eleven categories of equipment, narrowing "
            "what may ship without a licence."
        ),
    ),
    sources=(Source("Export list updated", "https://example.test/chip-1",
                    publisher="Example Daily", date="2026-09-16"),),
    topic="Chip export rules widen",
)

BRIEF = Document(
    kicker="Daily brief",
    headline="Regulators move on rules for AI and chips",
    deck="Two regulatory fronts advanced today, both with compliance deadlines inside six months.",
    sections=(TOPIC_ONE, TOPIC_TWO),
    footer_note="You receive this brief because the interest 'AI News' is configured for your account.",
    preheader="Two regulatory fronts advanced today.",
    subject="AI News Daily Brief",
)

THEME = Document(
    kicker="AI News · theme 2 of 5",
    headline="EU tightens AI rules",
    deck="Member states approved the final implementing text; enforcement starts in March.",
    sections=(
        Section(
            heading="Summary",
            blocks=(Paragraph("Regulators cleared the final text on Tuesday."),),
            sources=TOPIC_ONE.sources,
        ),
    ),
    footer_note="You receive this report because the interest 'AI News' is configured for your account.",
)

# CSS that a target client either ignores or that makes Gmail drop a style.
BANNED_CSS = (
    "display:flex", "display:grid", "position:", "float:", "background-image",
    "url(", "@font-face", "@import",
)


def _luminance(color: str) -> float:
    """WCAG relative luminance of a #RRGGBB color."""
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(fg: str, bg: str) -> float:
    high, low = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.fixture(params=[BRIEF, THEME], ids=["brief", "theme"])
def doc(request):
    return request.param


@pytest.fixture
def rendered(doc):
    return render_html(doc)


# ---- structure ---------------------------------------------------------------


def test_html_parses_with_a_single_document_head_and_h1(rendered, doc):
    root = lxml_html.fromstring(rendered)
    assert rendered.startswith("<!DOCTYPE html>")
    assert root.tag == "html"
    assert root.get("lang") == doc.language
    assert len(root.findall(".//h1")) == 1
    assert root.findall(".//body") and root.findall(".//title")
    heading_tags = [e.tag for e in root.iter() if e.tag in ("h1", "h2")]
    assert heading_tags[0] == "h1"
    assert heading_tags.count("h2") == sum(1 for s in doc.sections if s.heading)


def test_every_layout_table_is_a_presentation_table(rendered):
    root = lxml_html.fromstring(rendered)
    tables = root.findall(".//table")
    assert len(tables) >= 2
    for table in tables:
        assert table.get("role") == "presentation", "layout table needs role=presentation"
        assert table.get("cellpadding") == "0"
        assert table.get("cellspacing") == "0"
        assert table.get("border") == "0"
        assert table.get("width"), "every table carries a width attribute"


def test_container_is_a_600px_fluid_table(rendered):
    assert f'width="{CONTENT_WIDTH}"' in rendered
    assert f"max-width:{CONTENT_WIDTH}px" in rendered
    # Outlook desktop honours max-width on a table only.
    assert rendered.index(f'width="{CONTENT_WIDTH}"') < rendered.index(
        f"max-width:{CONTENT_WIDTH}px"
    )


def test_no_banned_css_constructs(rendered):
    lowered = rendered.lower()
    for token in BANNED_CSS:
        assert token not in lowered, f"{token!r} breaks at least one target client"


def test_no_div_based_layout(rendered):
    for tag in re.findall(r"<div[^>]*>", rendered):
        style = re.search(r'style="([^"]*)"', tag)
        if not style:
            continue
        # A bare width (as opposed to max-width on a text container) means the
        # layout was built with divs, which Outlook desktop flattens.
        assert not re.search(r"(?<!max-)width:", style.group(1)), (
            f"div carrying a layout width: {tag}"
        )
        assert "display:flex" not in style.group(1).lower()


def test_spacing_comes_from_cell_padding_not_margins(rendered):
    # Outlook desktop ignores margin — no text block may rely on it.
    for tag in re.findall(r"<(?:p|h1|h2|ul|ol|li)[^>]*>", rendered):
        style = re.search(r'style="([^"]*)"', tag)
        if not style:
            continue
        margins = re.findall(r"margin:([^;]+)", style.group(1))
        assert all(value.strip() == "0" for value in margins), f"non-zero margin: {tag}"
    for tag in re.findall(r"<td[^>]*>", rendered):
        if 'bgcolor="' in tag:  # the 1 px divider cell, not a content cell
            continue
        assert "padding:" in tag, f"content cell without padding: {tag}"


def test_dividers_are_table_rows_not_hr(rendered):
    assert "<hr" not in rendered
    assert f'bgcolor="{RULE}"' in rendered


def test_dark_mode_block_lives_in_head_only(rendered):
    head, _, body = rendered.partition("</head>")
    assert "prefers-color-scheme" in head
    assert "prefers-color-scheme" not in body
    # Gmail ignores a <style> element that appears inside the body.
    assert "<style>" not in body


def test_preheader_is_the_first_body_child_and_short(rendered, doc):
    root = lxml_html.fromstring(rendered)
    body = root.findall(".//body")[0]
    first = body[0]
    assert first.tag == "div"
    style = first.get("style", "")
    assert "display:none" in style and "max-height:0" in style
    text = " ".join((first.text_content() or "").split())
    assert 0 < len(text) <= PREHEADER_MAX
    assert text == (doc.preheader or doc.deck or doc.headline)[:PREHEADER_MAX]


# ---- color, contrast, payload ------------------------------------------------


def test_only_palette_colors_are_used(rendered):
    palette = {color.lower() for pair in CONTRAST_PAIRS for color in pair}
    palette |= {RULE.lower(), *(c.lower() for c in DARK_PALETTE)}
    used = {c.lower() for c in re.findall(r"#[0-9A-Fa-f]{6}", rendered)}
    assert used <= palette, f"off-palette colors: {sorted(used - palette)}"
    # Pure black is never used, and white only as the card surface — Gmail
    # inverts white↔black, so white *text* would invert into the background.
    assert "#000000" not in used
    assert not re.search(r"color:\s*#FFFFFF", rendered, re.IGNORECASE)


def test_palette_pairs_pass_wcag_aa():
    for fg, bg in CONTRAST_PAIRS:
        ratio = _contrast(fg, bg)
        assert ratio >= 4.5, f"{fg} on {bg} is {ratio:.2f}:1"


def test_links_carry_an_inline_color(rendered):
    anchors = re.findall(r"<a [^>]*>", rendered)
    assert anchors, "the report cites sources"
    for anchor in anchors:
        style = re.search(r'style="([^"]*)"', anchor)
        assert style and f"color:{LINK}" in style.group(1), anchor
        assert re.search(r'href="https?://', anchor), anchor


def test_anchor_text_is_descriptive_not_a_url(rendered):
    root = lxml_html.fromstring(rendered)
    anchors = root.findall(".//a")
    assert anchors
    for anchor in anchors:
        text = " ".join((anchor.text_content() or "").split())
        assert text and text.lower() not in {"here", "click here", "link", "read more"}
        assert not text.startswith("http"), "a source link must carry its title"


def test_payload_stays_well_under_gmails_clip_limit(rendered):
    assert len(rendered.encode("utf-8")) < 80_000


# ---- per-topic attribution ---------------------------------------------------


def test_sources_sit_under_their_own_topic_in_html():
    rendered = render_html(BRIEF)
    first_topic = rendered.index("EU tightens AI rules")
    second_topic = rendered.index("Chip export rules widen")
    assert first_topic < second_topic
    # Topic one's links appear in topic one's block…
    assert "https://example.test/ai-1" in rendered[first_topic:second_topic]
    assert "https://example.test/ai-2" in rendered[first_topic:second_topic]
    # …and never in topic two's, so no link can land on the wrong topic.
    assert "https://example.test/chip-1" in rendered[second_topic:]
    assert "https://example.test/ai-1" not in rendered[second_topic:]
    # No pooled list at the end of the report.
    assert rendered.count(">Sources<") == 2


def test_text_part_mirrors_html_and_wraps():
    text = render_text(BRIEF)
    assert BRIEF.headline in text
    assert "EU TIGHTENS AI RULES" in text
    assert "CHIP EXPORT RULES WIDEN" in text
    assert "* High-risk systems must register with national supervisors" in text
    # Source entries: '- Title' and, when the attribution does not fit beside
    # the URL, the link on its own continuation line — never a bare URL.
    assert "- AI act clears final hurdle" in text
    assert "https://example.test/ai-1 (Example Wire, 2026-09-16)" in text
    assert "<" not in text and ">" not in text  # no stray markup
    long_lines = [line for line in text.splitlines()
                  if len(line) > TEXT_WIDTH and "http" not in line]
    assert not long_lines, long_lines
    # The same anchors as the HTML part — the text is not a degraded copy.
    html = render_html(BRIEF)
    assert text.count("https://example.test/") == html.count("https://example.test/")


def test_text_sources_are_attributed_per_topic():
    text = render_text(BRIEF)
    first = text.index("EU TIGHTENS AI RULES")
    second = text.index("CHIP EXPORT RULES WIDEN")
    assert "https://example.test/ai-1" in text[first:second]
    assert "https://example.test/chip-1" in text[second:]
    assert "https://example.test/ai-1" not in text[second:]


def test_markdown_archive_is_a_sibling_rendering():
    markdown = BRIEF.to_markdown()
    assert markdown.startswith("*Daily brief*")
    assert "# Regulators move on rules for AI and chips" in markdown
    assert "## EU tightens AI rules" in markdown
    assert ("1. [AI act clears final hurdle](https://example.test/ai-1)"
            " — Example Wire, 2026-09-16") in markdown
    assert "- High-risk systems must register with national supervisors" in markdown
    assert "<table" not in markdown and "<p" not in markdown


def test_document_round_trips_through_dicts():
    assert Document.from_dict(BRIEF.to_dict()) == BRIEF
    assert Document.from_dict(THEME.to_dict()) == THEME


def test_word_count_counts_prose_not_sources():
    count = BRIEF.word_count()
    assert count > 20
    assert count < len(render_html(BRIEF).split())  # markup is not prose
