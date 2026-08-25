"""Guard tests for LLM theme-array extraction (invariant G5's analyze
stage depends on it): a parseable array must be recovered from fenced,
prose-wrapped, bracket-cited, or object-wrapped model responses, and junk
output must raise instead of silently yielding zero themes.
"""

# version: 0.2.1

import pytest

from newspipeline.pipeline import _extract_json_array

ARRAY = '[{"title": "T1", "description": "d", "article_indices": [1, 2]}]'


def test_plain_array():
    parsed = _extract_json_array(ARRAY)
    assert parsed == [{"title": "T1", "description": "d", "article_indices": [1, 2]}]


def test_markdown_fenced_array():
    fenced = f"```json\n{ARRAY}\n```"
    assert _extract_json_array(fenced)[0]["title"] == "T1"


def test_fenced_array_without_newline_after_tag():
    fenced = f"```json {ARRAY}```"
    assert _extract_json_array(fenced)[0]["title"] == "T1"


def test_prose_wrapped_array():
    wrapped = f"Here are the themes you asked for:\n\n{ARRAY}\n\nLet me know!"
    assert _extract_json_array(wrapped)[0]["title"] == "T1"


def test_object_wrapped_array():
    # Common real-world shape; the old greedy regex accepted the inner array.
    wrapped = f'{{"themes": {ARRAY}, "notes": "done"}}'
    assert _extract_json_array(wrapped)[0]["title"] == "T1"


def test_citation_brackets_before_real_array():
    # The prompt invites "[N]" citations, so prose before the array often
    # contains balanced brackets that must NOT win over the real themes.
    prose = (
        "Based on articles [1] and [2], and the 2024 report [3], "
        f"the major themes are:\n\n{ARRAY}"
    )
    assert _extract_json_array(prose)[0]["title"] == "T1"


def test_unpaired_prose_quote_does_not_poison_scan():
    prose = f'She said "look below:\n{ARRAY}'
    assert _extract_json_array(prose)[0]["title"] == "T1"


def test_brackets_inside_strings_do_not_break_scan():
    tricky = '[{"title": "Array [1] and {brace} inside \\"quotes\\"", "description": "x"}]'
    assert _extract_json_array(tricky)[0]["title"] == "Array [1] and {brace} inside \"quotes\""


def test_bracket_inside_string_value_does_not_start_region():
    wrapped = '{"note": "see [x] here", "themes": [{"title": "ok"}]}'
    assert _extract_json_array(wrapped) == [{"title": "ok"}]


def test_nested_objects_survive():
    nested = '[{"title": "n", "meta": {"a": [1, {"b": 2}]}}]'
    assert _extract_json_array(nested)[0]["meta"]["a"][1]["b"] == 2


def test_non_dict_elements_are_dropped():
    mixed = '[{"junk": 1}, 42, {"title": "keep"}]'
    parsed = _extract_json_array(mixed)
    # Non-dict elements (42) are dropped; dicts are kept as-is.
    assert parsed == [{"junk": 1}, {"title": "keep"}]


def test_literal_empty_array_is_legitimate():
    assert _extract_json_array("[]") == []
    assert _extract_json_array("No significant themes found:\n[]") == []


@pytest.mark.parametrize(
    "bad", ["", "no json here", '{"title": "object without inner array"}', '[1, 2, 3]', '["x"]'],
)
def test_unparseable_or_junk_raises(bad):
    with pytest.raises(ValueError):
        _extract_json_array(bad)


def test_unclosed_array_raises():
    with pytest.raises(ValueError):
        _extract_json_array('[{"title": "truncated"')


def test_analyze_with_garbage_response_completes_partial(store, monkeypatch, tmp_path):
    """Integration pin: an unparseable analysis response must be logged and
    swallowed — the run continues (zero themes), it never crashes."""
    import asyncio

    import newspipeline.pipeline as pipeline_mod
    from newspipeline.pipeline import Pipeline

    class _GarbageLLM:
        def __init__(self, **kwargs):
            pass

        async def chat_completion(self, **kwargs):
            return "I'm sorry, I cannot produce JSON right now."

        async def close(self):
            pass

    monkeypatch.setattr(pipeline_mod, "LLMClient", _GarbageLLM)
    settings = {"global": {"api_base_url": "http://x", "default_model": "m"}}
    interest = {"name": "g", "feeds": [], "max_themes": 5}
    rid = store.create_run("g")["id"]
    store.insert_article(
        run_id=rid, feed_name="f", url="http://x/a", title="A",
        author=None, published_at="", excerpt="e", content="c",
        content_status="full",
    )

    pipeline = Pipeline(store=store, settings=settings, api_key="k")
    asyncio.run(pipeline._analyze(rid, interest, _GarbageLLM()))
    assert store.get_themes_for_run(rid) == []


def test_analyze_with_raising_llm_completes_partial(store, monkeypatch):
    """The response=None path: an LLM-call exception must be logged and
    swallowed exactly like unparseable output — never a NameError."""
    import asyncio

    import newspipeline.pipeline as pipeline_mod
    from newspipeline.pipeline import Pipeline

    class _ExplodingLLM:
        def __init__(self, **kwargs):
            pass

        async def chat_completion(self, **kwargs):
            raise RuntimeError("endpoint down")

        async def close(self):
            pass

    settings = {"global": {"api_base_url": "http://x", "default_model": "m"}}
    interest = {"name": "g", "feeds": [], "max_themes": 5}
    rid = store.create_run("g")["id"]
    store.insert_article(
        run_id=rid, feed_name="f", url="http://x/a", title="A",
        author=None, published_at="", excerpt="e", content="c",
        content_status="full",
    )

    pipeline = Pipeline(store=store, settings=settings, api_key="k")
    asyncio.run(pipeline._analyze(rid, interest, _ExplodingLLM()))
    assert store.get_themes_for_run(rid) == []
