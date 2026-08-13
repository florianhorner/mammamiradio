"""Unit tests for the shared Normal Mode language policy."""

from __future__ import annotations

from mammamiradio.hosts.language_policy import (
    NORMAL_MODE_ENGLISH_MAX,
    NORMAL_MODE_ENGLISH_MIN,
    NORMAL_MODE_ENGLISH_TARGET,
    assess_language,
    normal_mode_language_ok,
)


def test_assessment_counts_only_classified_language_words():
    assessment = assess_language("The song is back, ciao amici — Vivaldi")

    # The host hot-reload route can replace the module's dataclass identity;
    # assert the stable public shape rather than an import-time class object.
    assert type(assessment).__name__ == "LanguageAssessment"
    assert assessment.total_tokens == 7
    assert assessment.english_tokens == 4
    assert assessment.italian_tokens == 2
    assert assessment.unclassified_tokens == 1
    assert assessment.classified_tokens == 6
    assert assessment.english_share == 4 / 6
    assert assessment.italian_share == 2 / 6


def test_ambiguous_short_words_do_not_turn_english_copy_italian():
    assessment = assess_language("I am in the room, ciao")

    assert assessment.english_tokens == 2
    assert assessment.italian_tokens == 1


def test_long_copy_accepts_the_75_percent_target():
    text = "The music is back and we stay with the song, ciao amici grazie"
    assessment = assess_language(text)

    assert NORMAL_MODE_ENGLISH_TARGET == 0.75
    assert NORMAL_MODE_ENGLISH_MIN <= assessment.english_share <= NORMAL_MODE_ENGLISH_MAX
    assert normal_mode_language_ok(text)


def test_long_copy_rejects_italian_heavy_text():
    text = "The music is back, ciao amici grazie adesso allora bene"

    assert assess_language(text).english_share < NORMAL_MODE_ENGLISH_MIN
    assert not normal_mode_language_ok(text)


def test_long_copy_accepts_exact_english_floor():
    text = "The music is back and we stay, ciao amici grazie"

    assert assess_language(text).english_share == NORMAL_MODE_ENGLISH_MIN
    assert normal_mode_language_ok(text)


def test_long_copy_rejects_text_just_below_the_english_floor():
    text = "The music is back and we stay here, ciao amici grazie bene"
    assessment = assess_language(text)

    assert assessment.is_short is False
    assert assessment.english_share < NORMAL_MODE_ENGLISH_MIN
    assert not normal_mode_language_ok(text)


def test_long_copy_accepts_english_heavy_text_above_target_band():
    text = "The music is back and we stay with the song tonight, ciao"

    assert assess_language(text).english_share > NORMAL_MODE_ENGLISH_MAX
    assert normal_mode_language_ok(text)


def test_long_copy_accepts_english_only_text():
    """The guard is one-sided by design: it turns back Italian-heavy copy only.

    Keeping Italian present is the prompt's job (and is reported separately as
    ``within_preferred_band`` telemetry), not this guard's — rejecting valid
    English-led dialogue is what collapsed live host exchanges into stock copy.
    """
    text = "The music is back and we stay with the song tonight"
    assessment = assess_language(text)

    assert assessment.is_short is False
    assert assessment.italian_tokens == 0
    assert assessment.english_share == 1.0
    assert normal_mode_language_ok(text)


def test_short_copy_cannot_bypass_all_italian_output():
    assert not normal_mode_language_ok("Ciao amici, grazie")
    assert normal_mode_language_ok("Back, ciao")


def test_unclassified_nonempty_copy_fails_closed():
    assert not normal_mode_language_ok("Vivaldi Primavera")


def test_super_italian_bypasses_normal_mode_ratio():
    assert normal_mode_language_ok("Ciao amici, grazie", super_italian=True)
