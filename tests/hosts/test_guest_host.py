"""Tests for the Hans Günther test-balloon guest-host handling.

Covers the `_regular_hosts` filter (used at every solo/fallback host pick so the
guest never carries a segment) and the `_guest_host_directive` brief (applied in
both language modes so the guest is never described to the LLM without a brief).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import HostPersonality, PersonalityAxes
from mammamiradio.hosts.scriptwriter import (
    _LOCAL_BALLOON_GUEST_HOST,
    _build_system_prompt,
    _guest_host_directive,
    _regular_hosts,
)


def _host(name: str, *, energy: int = 50, chaos: int = 50) -> HostPersonality:
    return HostPersonality(
        name=name,
        voice="it-IT-DiegoNeural",
        style="",
        personality=PersonalityAxes(energy=energy, chaos=chaos),
    )


@pytest.fixture()
def config():
    cfg = load_config()
    cfg.anthropic_api_key = "test-key"
    cfg.openai_api_key = ""
    return cfg


# ── _regular_hosts ───────────────────────────────────────────────────────────


def test_regular_hosts_drops_balloon_guest(config):
    """The guest is excluded even though his high energy would qualify him as a regular."""
    config.hosts = [
        _host("Giulia"),
        _host("Marco"),
        _host(_LOCAL_BALLOON_GUEST_HOST, energy=92, chaos=65),
    ]
    names = [h.name for h in _regular_hosts(config)]
    assert names == ["Giulia", "Marco"]
    assert _LOCAL_BALLOON_GUEST_HOST not in names


def test_regular_hosts_falls_back_when_only_guest(config):
    """If the guest is the only host, return him rather than an empty list (no IndexError)."""
    config.hosts = [_host(_LOCAL_BALLOON_GUEST_HOST, energy=92)]
    assert [h.name for h in _regular_hosts(config)] == [_LOCAL_BALLOON_GUEST_HOST]


def test_regular_hosts_returns_all_when_guest_absent(config):
    """Without the guest in the roster the full host list is returned unchanged."""
    config.hosts = [_host("Giulia"), _host("Marco")]
    assert [h.name for h in _regular_hosts(config)] == ["Giulia", "Marco"]


def test_regular_hosts_empty_roster(config):
    """An empty roster stays empty — callers handle the no-host case themselves."""
    config.hosts = []
    assert _regular_hosts(config) == []


def test_chaos_nomination_excludes_guest(config):
    """The guest must never be named in the chaos 'most volatile hosts' line.

    Mirrors the comprehension in `write_banter`: chaos hosts are drawn from
    `_regular_hosts`, so the guest (energy 92 qualifies him) is dropped before
    the threshold filter ever runs.
    """
    config.hosts = [
        _host("Giulia", energy=95),
        _host("Marco", chaos=88),
        _host(_LOCAL_BALLOON_GUEST_HOST, energy=92, chaos=65),
    ]
    chaos_hosts = [h.name for h in _regular_hosts(config) if h.personality.chaos >= 80 or h.personality.energy >= 90]
    assert chaos_hosts == ["Giulia", "Marco"]
    assert _LOCAL_BALLOON_GUEST_HOST not in chaos_hosts


# ── _guest_host_directive ────────────────────────────────────────────────────


def test_guest_directive_absent_without_guest(config):
    """No guest in the roster → no directive, in either mode."""
    config.hosts = [_host("Giulia"), _host("Marco")]
    config.super_italian_mode = False
    assert _guest_host_directive(config, super_italian=False) == ""
    assert "GUEST HOST" not in _build_system_prompt(config)


def test_guest_directive_present_in_both_modes(config):
    """The guest brief must be applied whether Super Italian is on or off.

    Regression guard: the brief used to live only in the code-switch (OFF) branch,
    so Super Italian mode listed the guest among the hosts with no guest framing.
    """
    config.hosts = [
        _host("Giulia"),
        _host("Marco"),
        _host(_LOCAL_BALLOON_GUEST_HOST, energy=92, chaos=65),
    ]
    for mode in (False, True):
        config.super_italian_mode = mode
        prompt = _build_system_prompt(config)
        assert "GUEST HOST — Hans Günther" in prompt
        assert "GUEST STAR" in prompt
        assert "available only when a specific banter prompt explicitly opens the guest-host gate" in prompt
        assert "he is not silent" not in prompt


def test_guest_directive_language_clause_tracks_mode(config):
    """The conversation-language clause is the only mode-dependent part of the brief."""
    config.hosts = [
        _host("Giulia"),
        _host("Marco"),
        _host(_LOCAL_BALLOON_GUEST_HOST, energy=92),
    ]
    off = _guest_host_directive(config, super_italian=False)
    on = _guest_host_directive(config, super_italian=True)
    assert "keep the station conversation mostly English with Italian colour" in off
    assert "keep the station conversation Italian;" in on
    assert "keep the station conversation mostly English with Italian colour" not in on


def test_guest_directive_empty_when_only_guest(config):
    """With no real regular hosts, emit no guest framing (it would point at himself)."""
    config.hosts = [_host(_LOCAL_BALLOON_GUEST_HOST, energy=92)]
    assert _guest_host_directive(config, super_italian=False) == ""
    assert _guest_host_directive(config, super_italian=True) == ""


def test_guest_directive_names_regular_hosts(config):
    """The brief hands the floor back to the regular hosts by name, never to the guest."""
    config.hosts = [
        _host("Giulia"),
        _host("Marco"),
        _host(_LOCAL_BALLOON_GUEST_HOST, energy=92),
    ]
    directive = _guest_host_directive(config, super_italian=False)
    assert "Giulia and Marco" in directive


def test_guest_directive_no_longer_seeds_coffee_as_default_bit(config):
    """Rare Hans cameos should not all collapse into the same tiny-cup joke."""
    config.hosts = [
        _host("Giulia"),
        _host("Marco"),
        _host(_LOCAL_BALLOON_GUEST_HOST, energy=92),
    ]
    directive = _guest_host_directive(config, super_italian=False)
    assert "thimble" not in directive
    assert "tiny-cup" not in directive
    assert "geh weida col caffè" not in directive
    assert "a bissl troppo piccolo" not in directive


def test_guest_config_style_caps_coffee_instead_of_defining_him(config):
    hans = next(h for h in config.hosts if h.name == _LOCAL_BALLOON_GUEST_HOST)
    assert "coffee jokes are occasional, never his default bit" in hans.style
    assert "thimble-sized espressos" not in hans.style


def test_root_and_addon_guest_config_styles_stay_in_sync():
    repo_root = Path(__file__).resolve().parents[2]

    def hans_style(path: str) -> str:
        data = tomllib.loads((repo_root / path).read_text())
        return next(host["style"] for host in data["hosts"] if host["name"] == _LOCAL_BALLOON_GUEST_HOST)

    root_style = hans_style("radio.toml")
    addon_style = hans_style("ha-addon/mammamiradio/radio.toml")
    assert root_style == addon_style
    assert "coffee jokes are occasional, never his default bit" in addon_style
    assert "thimble-sized espressos" not in addon_style


def test_regular_pairing_survives_guest_in_roster(config):
    """Adding the guest must not disable the two-regular energy/chaos foil.

    The relative pairing (one host leads chaos, the other cuts with 'surgical'
    contrast) only fires when both high-energy/high-chaos regulars are paired
    against each other. With a third roster entry it used to silently break,
    because pairing keyed off len(config.hosts) == 2.
    """
    config.hosts = [
        _host("Marco", energy=95, chaos=85),
        _host("Giulia", energy=92, chaos=88),
        _host(_LOCAL_BALLOON_GUEST_HOST, energy=92, chaos=65),
    ]
    with_guest = _build_system_prompt(config)
    assert "surgical" in with_guest  # the foil instruction survived the third host

    config.hosts = config.hosts[:2]
    without_guest = _build_system_prompt(config)
    assert "surgical" in without_guest  # same behavior as the pure two-host roster
