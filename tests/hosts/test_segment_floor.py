"""Tests for the deterministic raw script-output integrity receipt."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mammamiradio.hosts.segment_floor import check_floor


@pytest.fixture()
def config():
    return SimpleNamespace(
        display_station_name="Mamma Mi Radio",
        hosts=[
            SimpleNamespace(name="Marco"),
            SimpleNamespace(name="Giulia"),
            SimpleNamespace(name="Hans Günther"),
        ],
    )


@pytest.mark.parametrize(
    ("segment", "raw_output"),
    [
        ("banter", {"lines": [{"host": "Marco", "text": "Che bella canzone."}]}),
        ("ad", {"parts": [{"type": "voice", "text": "Una crema per il sole."}]}),
        ("ad", {"parts": [{"text": "Una voce senza tipo esplicito."}]}),
        ("news_flash", {"text": "Traffico intenso sulla A1."}),
        ("transition", {"text": "E ora torniamo alla musica."}),
    ],
)
def test_check_floor_passes_valid_spoken_shapes(config, segment, raw_output):
    result = check_floor(segment, raw_output, config)

    assert result.status == "PASS"
    assert result.station_name.status == "PASS"
    assert result.spoken_text.status == "PASS"


@pytest.mark.parametrize(
    ("segment", "raw_output"),
    [
        ("banter", {"lines": [{"host": "Marco", "text": "Radio Kiss Kiss vi saluta."}]}),
        ("ad", {"parts": [{"type": "voice", "text": "Radio Kiss Kiss vi saluta."}]}),
        ("news_flash", {"text": "Radio Kiss Kiss vi saluta."}),
        ("transition", {"text": "Radio Kiss Kiss vi saluta."}),
    ],
)
def test_check_floor_rejects_prefix_foreign_station_names(config, segment, raw_output):
    result = check_floor(segment, raw_output, config)

    assert result.status == "FAIL"
    assert result.station_name.status == "FAIL"
    assert result.station_name.reason == "foreign_station_name"


@pytest.mark.parametrize("host", ["marco ", "Marco:"])
def test_check_floor_roster_normalization_matches_live_path(config, host):
    result = check_floor("banter", {"lines": [{"host": host, "text": "Ci siamo."}]}, config)

    assert result.roster.status == "PASS"


def test_check_floor_accepts_configured_guest_first_name_alias(config):
    result = check_floor("banter", {"lines": [{"host": "Hans", "text": "Guten Abend."}]}, config)

    assert result.status == "PASS"
    assert result.roster.status == "PASS"


def test_check_floor_rejects_guest_first_name_alias_when_guest_is_disabled(config):
    config.hosts = [host for host in config.hosts if host.name != "Hans Günther"]

    result = check_floor("banter", {"lines": [{"host": "Hans", "text": "Guten Abend."}]}, config)

    assert result.status == "FAIL"
    assert result.roster.status == "FAIL"
    assert result.roster.reason == "unknown_host"


@pytest.mark.parametrize(
    ("host", "reason"),
    [("Sofia", "unknown_host"), ("Hans Guenther", "unknown_host"), (None, "missing_host"), ("", "missing_host")],
)
def test_check_floor_rejects_invalid_raw_banter_hosts(config, host, reason):
    result = check_floor("banter", {"lines": [{"host": host, "text": "Ci siamo."}]}, config)

    assert result.status == "FAIL"
    assert result.roster.status == "FAIL"
    assert result.roster.reason == reason


def test_check_floor_keeps_supported_string_banter_shape(config):
    result = check_floor("banter", {"lines": ["Una battuta senza etichetta."]}, config)

    assert result.status == "PASS"
    assert result.roster.status == "N/A"


def test_check_floor_keeps_supported_ad_root_text_fallback(config):
    result = check_floor("ad", {"parts": [{"type": "sfx", "sfx": "sweep"}], "text": "Una voce sola."}, config)

    assert result.status == "PASS"
    assert result.roster.status == "N/A"


def test_check_floor_keeps_ad_root_text_fallback_when_parts_are_absent(config):
    result = check_floor("ad", {"text": "Una voce sola."}, config)

    assert result.status == "PASS"
    assert result.roster.status == "N/A"


@pytest.mark.parametrize(
    "parts",
    [
        None,
        {"type": "voice", "text": "Una voce sola."},
        "not a list",
        [None],
        [{"type": "sfx", "sfx": "sweep"}, None],
    ],
)
def test_check_floor_rejects_malformed_ad_parts_even_with_root_text(config, parts):
    result = check_floor("ad", {"parts": parts, "text": "Una voce sola."}, config)

    assert result.status == "FAIL"
    assert result.spoken_text.status == "FAIL"
    assert result.spoken_text.reason == "no_spoken_text"


@pytest.mark.parametrize(
    ("segment", "raw_output"),
    [
        ("banter", {"lines": []}),
        ("banter", {"lines": [{"host": "Marco", "text": "  "}]}),
        # dict line / voice part whose text is present-but-not-a-string is not airable
        ("banter", {"lines": [{"host": "Marco", "text": 123}]}),
        ("ad", {"parts": [{"type": "voice", "text": "  "}]}),
        ("ad", {"parts": [{"type": "voice"}]}),
        ("ad", {"parts": [{"type": "sfx", "sfx": "sweep"}]}),
        ("news_flash", {}),
        ("transition", {"text": "  "}),
    ],
)
def test_check_floor_rejects_missing_spoken_text(config, segment, raw_output):
    result = check_floor(segment, raw_output, config)

    assert result.status == "FAIL"
    assert result.spoken_text.status == "FAIL"
    assert result.spoken_text.reason == "no_spoken_text"


@pytest.mark.parametrize(
    ("segment", "raw_output"),
    [
        ("direction", {"label": "Italian pop", "targets": [{"artist": "A", "title": "B"}]}),
        ("memory_extract", {"persona_updates": {}, "song_cues": []}),
    ],
)
def test_check_floor_marks_non_spoken_callers_not_applicable(config, segment, raw_output):
    result = check_floor(segment, raw_output, config)

    assert result.status == "N/A"
    assert result.to_dict() == {
        "status": "N/A",
        "gates": {
            "station_name": {"status": "N/A", "reason": "not_applicable"},
            "roster": {"status": "N/A", "reason": "not_applicable"},
            "spoken_text": {"status": "N/A", "reason": "not_applicable"},
        },
    }


def test_check_floor_documents_suffix_form_station_gap(config):
    result = check_floor("banter", {"lines": [{"host": "Marco", "text": "Benvenuti su Malamie Radio."}]}, config)

    assert result.status == "PASS"
    assert result.station_name.status == "PASS"


@pytest.mark.parametrize(
    ("segment", "raw_output"),
    [
        ("banter", None),
        ("banter", []),
        ("banter", {"lines": {"host": "Marco"}}),
        ("ad", {"parts": None}),
        ("news_flash", {"text": ["not text"]}),
        ("transition", {"text": {"not": "text"}}),
        (None, {"text": "ignored"}),
    ],
)
def test_check_floor_is_total_for_malformed_input(config, segment, raw_output):
    result = check_floor(segment, raw_output, config)

    assert result.status in {"PASS", "FAIL", "N/A"}
