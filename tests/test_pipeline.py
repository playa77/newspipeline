"""Guard tests for invariant G4: language-detection heuristic parity with
legacy.

Parity is tested against the actual legacy module (imported from legacy/
with the workbench package stubbed out), not a reimplementation.
"""

# version: 0.1.0

import pytest

from newspipeline.pipeline import detect_language

from conftest import load_legacy_module


@pytest.fixture(scope="module")
def legacy():
    return load_legacy_module(
        "legacy_news_pipeline", "news_pipeline.py", stub_workbench=True,
    )


def test_detect_language_german():
    text = (
        "Der Bundestag hat das neue Gesetz beschlossen, und die Wirtschaft "
        "ist nicht einverstanden. Es wird auch über Steuern gesprochen, "
        "dass viele sich Sorgen machen."
    )
    assert detect_language(text) == "de"


def test_detect_language_english():
    text = (
        "The government said that the policy is not final, and the markets "
        "are watching. This is the story of the week for many people who "
        "have an interest in the outcome."
    )
    assert detect_language(text) == "en"


def test_detect_language_empty_and_fallback():
    assert detect_language("") == "en"
    assert detect_language(None) == "en"
    # No recognizable words at all -> falls back to English.
    assert detect_language("xyzzy plugh qwfp") == "en"


def test_detect_language_threshold_boundary():
    # German hits must exceed English hits * 1.5 (strictly).
    de_words = "der die das und"          # 4 German
    en_words = "the and"                  # 2 English ("and" is English-only)
    assert detect_language(f"{de_words} {en_words}") == "de"   # 4 > 3
    borderline = "der die das"            # 3 German
    assert detect_language(f"{borderline} {en_words}") == "en"  # 3 not > 3


def test_detect_language_matches_legacy(legacy):
    texts = [
        "",
        "der die das und ist sind",
        "the and is are was were for with",
        "Der Markt und the policy ist mixed with the news",
        "Über die Zukunft wird gesprochen und nichts ist sicher",
        "a the an of to in on at by",
        "12345 !!! ???",
        "dass wenn aber oder weil the and",
    ]
    for text in texts:
        assert detect_language(text) == legacy.detect_language(text), text
