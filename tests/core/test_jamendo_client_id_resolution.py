"""Resolve the bundled Jamendo application ID and preserve operator overrides."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from mammamiradio.core import config as config_module

_RADIO_TOML = Path(__file__).resolve().parents[2] / "radio.toml"
_BUNDLED = "station-bundled-id"
_OPERATOR = "operator-own-id"


def _resolve(
    monkeypatch,
    *,
    bundled: str,
    client_id: str = "",
    enabled: bool = False,
    acknowledged: bool = False,
    ack_revision: str | None = None,
):
    """Load the real config and return its resolved playlist section."""
    monkeypatch.setattr(config_module, "BUNDLED_JAMENDO_CLIENT_ID", bundled)
    # The bundled value is validated once per distinct string so a malformed
    # constant cannot log on every /status poll. Reset it here, or a test that
    # asserts the warning goes silent whenever an earlier test used the same
    # value first.
    config_module._validated_bundled_client_id.cache_clear()
    monkeypatch.setenv("JAMENDO_CLIENT_ID", client_id)
    monkeypatch.setenv("MAMMAMIRADIO_JAMENDO_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv(
        "MAMMAMIRADIO_JAMENDO_NONCOMMERCIAL_ACKNOWLEDGED",
        "true" if acknowledged else "false",
    )
    monkeypatch.setenv(
        "MAMMAMIRADIO_JAMENDO_ACK_REVISION",
        config_module.JAMENDO_ACK_REVISION if ack_revision is None else ack_revision,
    )
    return config_module.load_config(str(_RADIO_TOML)).playlist


def test_operator_id_wins_over_the_bundled_one(monkeypatch) -> None:
    playlist = _resolve(monkeypatch, bundled=_BUNDLED, client_id=_OPERATOR)

    assert playlist.jamendo_client_id == _OPERATOR
    assert playlist.jamendo_client_id_source == "operator"


def test_malformed_operator_id_falls_back_instead_of_killing_the_source(monkeypatch, caplog) -> None:
    """An invalid operator ID falls back without disabling the source."""
    caplog.set_level(logging.WARNING)
    playlist = _resolve(
        monkeypatch,
        bundled=_BUNDLED,
        client_id="!! not a client id !!",
        enabled=True,
        acknowledged=True,
    )

    assert playlist.jamendo_client_id == _BUNDLED
    assert playlist.jamendo_client_id_source == "bundled"
    assert playlist.jamendo_enabled is True
    assert "falling back to the bundled station access" in caplog.text


def test_malformed_bundled_id_reads_as_absent(monkeypatch, caplog) -> None:
    """An invalid bundled ID is treated as absent."""
    caplog.set_level(logging.WARNING)
    playlist = _resolve(monkeypatch, bundled="nope!")

    assert playlist.jamendo_client_id == ""
    assert playlist.jamendo_client_id_source == ""
    assert "Bundled Jamendo client id is malformed" in caplog.text


def test_enabling_with_the_bundled_id_and_the_acknowledgement_sticks(monkeypatch) -> None:
    """The bundled ID works after the acknowledgement."""
    playlist = _resolve(monkeypatch, bundled=_BUNDLED, enabled=True, acknowledged=True)

    assert playlist.jamendo_enabled is True
    assert playlist.jamendo_client_id == _BUNDLED
    assert playlist.jamendo_client_id_source == "bundled"


def test_the_shipped_bundled_client_id_is_empty_or_usable() -> None:
    """A typo in the shipped constant degrades every install to operator-only.

    Nothing else notices: the resolver treats a malformed value as absent, which
    is the same code path as the supported "no bundled access" configuration, so
    the feature silently stops working with only a boot warning to show for it.
    """
    raw = config_module.BUNDLED_JAMENDO_CLIENT_ID
    assert raw == raw.strip(), "shipped client id has surrounding whitespace"
    assert raw == "" or config_module.bundled_jamendo_client_id() == raw, (
        f"shipped BUNDLED_JAMENDO_CLIENT_ID {raw!r} fails the format gate, so every "
        "install silently falls back to operator-only Jamendo access"
    )


def test_the_client_id_pattern_rejects_a_trailing_newline() -> None:
    """``$`` also matches before a final newline; the retired pattern used fullmatch.

    Every caller strips today, so this is not reachable, but the constant is now
    the shared contract for three modules and the next one may not strip.
    """
    assert config_module.JAMENDO_CLIENT_ID_RE.match("abcdefgh\n") is None
    assert config_module.JAMENDO_CLIENT_ID_RE.match("abcdefgh") is not None


@pytest.mark.parametrize(
    ("bundled", "operator", "expected"),
    [
        ("station-bundled-id", "", ("station-bundled-id", "bundled")),
        ("station-bundled-id", "operator-own-id", ("operator-own-id", "operator")),
        ("station-bundled-id", "   ", ("station-bundled-id", "bundled")),
        ("station-bundled-id", "  operator-own-id  ", ("operator-own-id", "operator")),
        ("station-bundled-id", "bad!", ("station-bundled-id", "bundled")),
        # Operator-only install with a typo: nothing resolves, and the enable
        # gate downstream is what keeps the source off.
        ("", "bad!", ("", "")),
        ("", "", ("", "")),
    ],
)
def test_resolver_table(bundled, operator, expected, monkeypatch) -> None:
    monkeypatch.setattr(config_module, "BUNDLED_JAMENDO_CLIENT_ID", bundled)
    config_module._validated_bundled_client_id.cache_clear()
    assert config_module.resolve_jamendo_client_id(operator) == expected


@pytest.mark.parametrize("config", [SimpleNamespace(), SimpleNamespace(playlist=None)])
def test_source_predicate_survives_a_config_without_a_playlist(config) -> None:
    assert config_module.jamendo_source_configured(config) is False


def test_a_malformed_bundled_id_warns_once_not_on_every_call(monkeypatch, caplog) -> None:
    """/status polls this every 3s; an unconditional warning is a Pi log flood."""
    monkeypatch.setattr(config_module, "BUNDLED_JAMENDO_CLIENT_ID", "nope!")
    config_module._validated_bundled_client_id.cache_clear()
    caplog.set_level(logging.WARNING)

    for _ in range(5):
        assert config_module.bundled_jamendo_client_id() == ""

    assert caplog.text.count("Bundled Jamendo client id is malformed") == 1
