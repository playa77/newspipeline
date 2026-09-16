"""Guard tests for stage-output → document composition (compose.py).

These pin the two things the HTML-first design depends on: the model's JSON
becomes structure (never prose to be re-parsed later), and a run never fails
or loses a source because a model answered in the wrong shape. The per-topic
attribution of brief sections — the reason the brief exists as a document —
is asserted here and end-to-end in test_e2e.py.
"""

# version: 0.1.0

import json

from newspipeline import compose
from newspipeline.document import Bullets, Document, Paragraph, Section, Source


class _StubStore:
    """Minimal stand-in for Store.get_articles_by_ids."""

    def __init__(self, articles):
        self._articles = articles

    def get_articles_by_ids(self, run_id, article_ids):
        out = []
        for aid in article_ids:
            if 1 <= aid <= len(self._articles):
                out.append(self._articles[aid - 1])
        return out


ARTICLES = [
    {"title": "Article One", "url": "http://example.test/a1",
     "feed_name": "Fixture Feed", "published_at": "2026-08-08T06:00:00+00:00"},
    {"title": "Article Two", "url": "http://example.test/a2",
     "feed_name": "Fixture Feed", "published_at": "2026-08-08T07:30:00+00:00"},
]

THEMES = [
    {"title": "Renewable Energy Surge", "description": "Investment accelerates."},
    {"title": "Grid Storage Policy", "description": "Storage economics shift."},
]


# ---- JSON extraction ---------------------------------------------------------


def test_extracts_a_bare_json_object():
    assert compose.extract_json_object('{"headline": "x"}') == {"headline": "x"}


def test_extracts_json_from_a_code_fence_and_from_prose():
    fenced = '```json\n{"headline": "x"}\n```'
    assert compose.extract_json_object(fenced) == {"headline": "x"}
    chatty = 'Sure! Here it is:\n{"headline": "x"}\nHope that helps.'
    assert compose.extract_json_object(chatty) == {"headline": "x"}


def test_returns_none_for_prose_or_arrays():
    assert compose.extract_json_object("no json here at all") is None
    assert compose.extract_json_object("[1, 2, 3]") is None
    assert compose.extract_json_object("") is None


# ---- sources -----------------------------------------------------------------


def test_resolve_sources_carries_publisher_date_and_dedupes():
    store = _StubStore(ARTICLES)
    sources = compose.resolve_sources(store, 1, [1, 2, 1])
    assert [s.title for s in sources] == ["Article One", "Article Two"]
    assert sources[0].publisher == "Fixture Feed"
    assert sources[0].date == "2026-08-08"
    assert sources[0].attribution == "Fixture Feed, 2026-08-08"


def test_resolve_sources_skips_articles_without_a_url():
    store = _StubStore([{"title": "No url", "url": ""}])
    assert compose.resolve_sources(store, 1, [1]) == ()


def test_resolve_sources_skips_out_of_range_indices():
    store = _StubStore(ARTICLES)
    assert compose.resolve_sources(store, 1, [7]) == ()


# ---- summary -----------------------------------------------------------------


def test_summary_document_keeps_the_models_structure():
    document = compose.summary_document(
        payload={"headline": "Rates hold", "deck": "Officials paused.",
                 "paragraphs": ["First.", "Second."]},
        raw="",
        theme_title="Theme title",
        theme_description="Theme description",
    )
    assert document.headline == "Rates hold"
    assert document.deck == "Officials paused."
    assert document.sections[0].blocks == (Paragraph("First."), Paragraph("Second."))
    assert document.notes == ()


def test_summary_document_falls_back_to_prose_without_losing_it():
    raw = "A headline line\n\nFirst paragraph.\n\nSecond paragraph."
    document = compose.summary_document(
        payload=None, raw=raw,
        theme_title="Theme title", theme_description="Theme description",
    )
    assert document.headline == "Theme title"       # nothing to take from the model
    assert document.deck == "Theme description"
    assert [b.text for b in document.sections[0].blocks] == [
        "A headline line", "First paragraph.", "Second paragraph.",
    ]
    assert document.notes == (compose.FALLBACK_NOTE,)


def test_summary_document_rejects_a_json_answer_without_paragraphs():
    document = compose.summary_document(
        payload={"headline": "Rates hold"}, raw="Some prose.",
        theme_title="Theme title", theme_description="Theme description",
    )
    assert document.headline == "Rates hold"
    assert [b.text for b in document.sections[0].blocks] == ["Some prose."]
    assert document.notes == (compose.FALLBACK_NOTE,)


# ---- script ------------------------------------------------------------------


def test_script_document_maps_sections_to_headings():
    document = compose.script_document(
        payload={"sections": [
            {"heading": "Cold open", "paragraphs": ["Hook."]},
            {"heading": "Outro", "paragraphs": ["Bye."]},
        ]},
        raw="",
    )
    assert [s.heading for s in document.sections] == ["Cold open", "Outro"]
    assert document.sections[0].blocks == (Paragraph("Hook."),)
    assert document.notes == ()


def test_script_document_falls_back_to_prose():
    document = compose.script_document(payload=None, raw="Just prose here.")
    assert document.sections[0].heading == ""
    assert [b.text for b in document.sections[0].blocks] == ["Just prose here."]
    assert document.notes == (compose.FALLBACK_NOTE,)


# ---- brief: the per-topic attribution ----------------------------------------


def _sources_by_theme():
    store = _StubStore(ARTICLES)
    return {
        1: compose.resolve_sources(store, 1, [1, 2]),
        2: compose.resolve_sources(store, 1, [1]),
    }


def test_brief_binds_each_sections_sources_to_its_own_theme():
    document = compose.brief_document(
        payload={
            "headline": "Regulators move",
            "deck": "Two fronts advanced.",
            "sections": [
                {"theme_number": 1, "heading": "Energy", "paragraphs": ["One."]},
                {"theme_number": 2, "heading": "Storage", "paragraphs": ["Two."]},
            ],
            "what_to_watch": ["Will it hold?"],
        },
        raw="",
        themes=THEMES,
        sources_by_theme=_sources_by_theme(),
        interest_name="AI News",
        run_date="2026-09-16",
    )
    first, second, watch = document.sections
    assert first.topic == "Renewable Energy Surge"
    assert [s.url for s in first.sources] == ["http://example.test/a1",
                                             "http://example.test/a2"]
    assert second.topic == "Grid Storage Policy"
    assert [s.url for s in second.sources] == ["http://example.test/a1"]
    assert watch.heading == "What to watch"
    assert watch.blocks == (Bullets(("Will it hold?",)),)
    assert watch.sources == ()
    assert document.kicker == "AI News · Daily Brief · 2026-09-16"
    assert document.headline == "Regulators move"


def test_brief_keeps_the_sources_of_a_theme_the_model_never_mentioned():
    document = compose.brief_document(
        payload={"headline": "h", "sections": [
            {"theme_number": 1, "heading": "Energy", "paragraphs": ["One."]},
        ]},
        raw="",
        themes=THEMES,
        sources_by_theme=_sources_by_theme(),
        interest_name="AI News",
    )
    headings = [s.heading for s in document.sections]
    assert headings == ["Energy", "Grid Storage Policy"]
    orphan = document.sections[1]
    assert [s.url for s in orphan.sources] == ["http://example.test/a1"]
    assert orphan.topic == "Grid Storage Policy"
    assert orphan.blocks == ()


def test_brief_ignores_a_theme_number_that_does_not_exist():
    document = compose.brief_document(
        payload={"headline": "h", "sections": [
            {"theme_number": 9, "heading": "Nonsense", "paragraphs": ["Text."]},
        ]},
        raw="",
        themes=THEMES,
        sources_by_theme=_sources_by_theme(),
        interest_name="AI News",
    )
    # The section survives (its text is the model's), but it is bound to no
    # topic and therefore carries no sources…
    assert document.sections[0].sources == ()
    assert document.sections[0].topic == ""
    # …and both themes still get their own sources at the end.
    assert [s.heading for s in document.sections[1:]] == [
        "Renewable Energy Surge", "Grid Storage Policy",
    ]


def test_brief_without_structure_falls_back_and_still_attributes_sources():
    document = compose.brief_document(
        payload=None,
        raw="A brief written as prose.",
        themes=THEMES,
        sources_by_theme=_sources_by_theme(),
        interest_name="AI News",
    )
    assert document.notes == (compose.FALLBACK_NOTE,)
    assert [b.text for b in document.sections[0].blocks] == ["A brief written as prose."]
    # No theme was tied to a section, so every theme keeps a sources block.
    assert [s.heading for s in document.sections[1:]] == [
        "Renewable Energy Surge", "Grid Storage Policy",
    ]


# ---- theme report assembly ---------------------------------------------------


def test_theme_document_with_one_deliverable_adds_no_section_heading():
    summary = compose.summary_document(
        payload={"headline": "Rates hold", "deck": "Officials paused.",
                 "paragraphs": ["First."]},
        raw="", theme_title="Theme title", theme_description="Theme description",
    )
    document = compose.theme_document(
        interest_name="AI News", theme=THEMES[0], position=2, total=5,
        summary=summary, sources=_sources_by_theme()[1],
    )
    assert [s.heading for s in document.sections] == [""]
    assert document.headline == "Rates hold"
    assert document.kicker == "AI News · theme 2 of 5"
    # A single-topic report has exactly one source list, at the end.
    assert [s.url for s in document.sections[-1].sources] == [
        "http://example.test/a1", "http://example.test/a2",
    ]
    assert document.footer_note == compose.footer_note("AI News")


def test_theme_document_labels_multiple_deliverables():
    summary = compose.summary_document(
        payload={"headline": "Rates hold", "deck": "d", "paragraphs": ["First."]},
        raw="", theme_title="t", theme_description="d",
    )
    script = compose.script_document(
        payload={"sections": [{"heading": "Cold open", "paragraphs": ["Hook."]}]},
        raw="",
    )
    document = compose.theme_document(
        interest_name="AI News", theme=THEMES[0], position=1, total=2,
        summary=summary, script=script, sources=_sources_by_theme()[1],
    )
    assert [s.heading for s in document.sections] == [
        "Summary", "Script (English)", "Cold open",
    ]
    assert [s.url for s in document.sections[-1].sources] == [
        "http://example.test/a1", "http://example.test/a2",
    ]


def test_theme_document_without_deliverables_keeps_the_sources():
    document = compose.theme_document(
        interest_name="AI News", theme=THEMES[0], position=1, total=1,
        sources=_sources_by_theme()[1],
    )
    assert [s.url for s in document.sections[0].sources] == [
        "http://example.test/a1", "http://example.test/a2",
    ]
    assert document.headline == "Renewable Energy Surge"


# ---- stored documents --------------------------------------------------------


def test_load_document_round_trips_a_stored_document():
    original = Document(
        kicker="AI News · Daily Brief",
        headline="Regulators move",
        deck="Two fronts.",
        sections=(Section(heading="Energy", blocks=(Paragraph("One."),),
                          sources=(Source("Article One", "http://example.test/a1"),),
                          topic="Renewable Energy Surge"),),
    )
    loaded = compose.load_document(json.dumps(original.to_dict()))
    assert loaded == original


def test_load_document_adds_the_footer_line_for_a_named_interest():
    stored = Document(headline="h", sections=(Section(blocks=(Paragraph("t"),)),))
    loaded = compose.load_document(json.dumps(stored.to_dict()), interest_name="AI News")
    assert loaded.footer_note == compose.footer_note("AI News")


def test_load_document_degrades_prose_to_a_rendered_document():
    loaded = compose.load_document("not json at all")
    assert loaded.notes == (compose.FALLBACK_NOTE,)
    assert [b.text for b in loaded.sections[0].blocks] == ["not json at all"]
