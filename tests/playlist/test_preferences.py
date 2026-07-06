"""Unit tests for persistent operator song preferences."""

from __future__ import annotations

import json

import pytest

from mammamiradio.playlist.preferences import (
    clear_preference,
    load_preferences,
    preference_meta,
    preference_score_map,
    preferences_path,
    save_preferences,
    set_preference,
)


def test_missing_file_returns_empty(tmp_path):
    assert load_preferences(tmp_path) == {}


@pytest.mark.parametrize("raw", ["{not valid json", json.dumps([1, 2, 3])])
def test_corrupt_or_non_dict_json_returns_empty(tmp_path, raw):
    preferences_path(tmp_path).write_text(raw, encoding="utf-8")
    assert load_preferences(tmp_path) == {}


def test_invalid_utf8_file_returns_empty(tmp_path):
    preferences_path(tmp_path).write_bytes(b"\xff\xfe not valid utf-8")
    assert load_preferences(tmp_path) == {}


def test_save_then_load_roundtrip_uses_unit_separator_keys(tmp_path):
    preferences = {
        ("mina", "se telefonando"): preference_meta(
            1,
            "Mina - Se telefonando",
            updated_at=123.0,
            updated_by="operator",
        ),
        ("vasco rossi", "albachiara"): preference_meta(
            -1,
            "Vasco Rossi - Albachiara",
            updated_at=456.0,
            updated_by="florian",
        ),
    }

    assert save_preferences(tmp_path, preferences) is True

    raw = json.loads(preferences_path(tmp_path).read_text(encoding="utf-8"))
    assert set(raw) == {"mina\x1fse telefonando", "vasco rossi\x1falbachiara"}

    loaded = load_preferences(tmp_path)
    assert loaded == preferences


def test_unicode_roundtrip(tmp_path):
    preferences = {
        ("måneskin", "zitti e buoni"): preference_meta(
            1,
            "Måneskin - Zitti e buoni - perché sì",
            updated_at=789.0,
            updated_by="opérator",
        )
    }

    assert save_preferences(tmp_path, preferences) is True
    assert "Måneskin" in preferences_path(tmp_path).read_text(encoding="utf-8")
    assert load_preferences(tmp_path) == preferences


@pytest.mark.parametrize("score", [-1, 1])
def test_preference_meta_accepts_only_valid_scores(score):
    assert preference_meta(score, updated_at=1.0)["score"] == score


@pytest.mark.parametrize("score", [-2, 0, 2, True, False])
def test_preference_meta_rejects_invalid_scores(score):
    with pytest.raises(ValueError):
        preference_meta(score)


def test_load_skips_invalid_rows(tmp_path):
    raw = {
        "mina\x1fse telefonando": {"score": 1, "display": "Mina - Se telefonando", "updated_at": 1.0},
        "bad score\x1fbad song": {"score": 0, "display": "Bad"},
        "missing score\x1fsong": {"display": "Missing"},
        "bad-key": {"score": -1, "display": "Bad Key"},
        "bad-meta\x1fsong": ["not", "a", "dict"],
    }
    preferences_path(tmp_path).write_text(json.dumps(raw), encoding="utf-8")

    loaded = load_preferences(tmp_path)

    assert loaded == {
        ("mina", "se telefonando"): {
            "score": 1,
            "display": "Mina - Se telefonando",
            "updated_at": 1.0,
            "updated_by": "operator",
        }
    }


def test_save_rejects_invalid_score_without_overwriting_existing_file(tmp_path):
    save_preferences(tmp_path, {("mina", "se telefonando"): preference_meta(1, updated_at=1.0)})
    before = preferences_path(tmp_path).read_text(encoding="utf-8")

    assert save_preferences(tmp_path, {("bad", "song"): {"score": 0}}) is False

    assert preferences_path(tmp_path).read_text(encoding="utf-8") == before


def test_save_is_atomic_no_leftover_temp_files(tmp_path):
    assert save_preferences(tmp_path, {("a", "b"): preference_meta(-1, "A - B", updated_at=1.0)}) is True
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".song-preferences-")]
    assert leftovers == []


def test_save_returns_false_on_write_failure(tmp_path, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("mammamiradio.playlist.preferences.tempfile.mkstemp", _boom)
    assert save_preferences(tmp_path, {("a", "b"): preference_meta(1, "A - B")}) is False


def test_set_preference_normalizes_metadata():
    preferences = {}

    meta = set_preference(
        preferences,
        ("mina", "se telefonando"),
        1,
        "Mina - Se telefonando",
        updated_at=123.0,
        updated_by="operator",
    )

    assert meta == {
        "score": 1,
        "display": "Mina - Se telefonando",
        "updated_at": 123.0,
        "updated_by": "operator",
    }
    assert preferences[("mina", "se telefonando")] == meta


def test_preference_score_map_normalizes_runtime_scores():
    preferences = {
        ("mina", "se telefonando"): {"score": 1},
        ("vasco", "albachiara"): {"score": -3},
        ("neutral", "ignored"): {"score": 0},
        ("bad", "ignored"): {"score": "not-a-number"},
    }

    assert preference_score_map(preferences) == {
        ("mina", "se telefonando"): 1,
        ("vasco", "albachiara"): -1,
    }


def test_clear_preference_reports_whether_row_was_removed():
    preferences = {("mina", "se telefonando"): preference_meta(1, updated_at=1.0)}

    assert clear_preference(preferences, ("mina", "se telefonando")) is True
    assert preferences == {}
    assert clear_preference(preferences, ("mina", "se telefonando")) is False
