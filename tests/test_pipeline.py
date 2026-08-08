"""Guard tests for invariant G4: language-detection heuristic semantics.

The reference outputs are frozen in reference_snapshots.py (captured from
the retired reference implementation before its removal on 2026-08-08).
"""

# version: 0.2.0

from newspipeline.pipeline import detect_language

from reference_snapshots import LANG


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


def test_detect_language_matches_frozen_reference():
    for text, expected in LANG.items():
        assert detect_language(text) == expected, text
