"""Tests for the Super Italian Mode branch in the scriptwriter system prompt."""

from __future__ import annotations

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.hosts.prompt_world import language_mode_rule
from mammamiradio.hosts.scriptwriter import _build_system_prompt, _get_system_prompt


def test_language_mode_rule_unmapped_code_degrades_not_keyerror():
    """The ON-mode rule must echo an unmapped language code raw, never KeyError.

    super_italian_mode and station.language are independent config fields, so
    Super Italian ON with station.language='de' is a reachable production config.
    _LANGUAGE_NAMES.get(code, code) is the documented guard — this pins it so a
    later refactor to _LANGUAGE_NAMES[code] can't reintroduce a KeyError inside
    a live prompt build.
    """
    assert language_mode_rule(True, "it") == "ALL text in Italian."
    assert language_mode_rule(True, "de") == "ALL text in de."
    assert language_mode_rule(True, "") == "ALL text in Italian."
    assert language_mode_rule(True, "   ") == "ALL text in Italian."
    # OFF ignores the code entirely — the static 75/25 rule, no map lookup.
    assert "75% English" in language_mode_rule(False, "de")


@pytest.fixture()
def config():
    cfg = load_config()
    cfg.anthropic_api_key = "test-key"
    cfg.openai_api_key = ""
    return cfg


def test_off_omits_full_italian_directive(config):
    """OFF mode must NOT promise full-Italian dialogue or full-immersion address."""
    config.super_italian_mode = False
    prompt = _build_system_prompt(config)
    assert "100% in Italian" not in prompt
    assert "amici miei" not in prompt


def test_off_targets_75_25_mix(config):
    """OFF mode is the international mix: 75/25 with real Italian flavor allowed."""
    config.super_italian_mode = False
    prompt = _build_system_prompt(config)
    assert "75% English" in prompt
    assert "25% Italian" in prompt
    assert "one word in four" in prompt


def test_on_demands_full_italian_directive(config):
    """ON mode must demand 100% Italian dialogue and full-immersion address."""
    config.super_italian_mode = True
    prompt = _build_system_prompt(config)
    assert "100% in Italian" in prompt
    assert "amici miei" in prompt
    assert "75% English" not in prompt


def test_cache_invalidates_on_mode_flip(config):
    """Toggling the mode must rebuild the cached prompt — otherwise hot-reload lies."""
    config.super_italian_mode = False
    off_prompt = _get_system_prompt(config)
    config.super_italian_mode = True
    on_prompt = _get_system_prompt(config)
    assert off_prompt != on_prompt
    assert "amici miei" in on_prompt
    assert "amici miei" not in off_prompt
