"""Tests for the Super Italian Mode branch in the scriptwriter system prompt."""

from __future__ import annotations

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.hosts.scriptwriter import _build_system_prompt, _get_system_prompt


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


def test_off_targets_70_30_mix(config):
    """OFF mode is the international mix: 70/30 with real Italian sentences allowed."""
    config.super_italian_mode = False
    prompt = _build_system_prompt(config)
    assert "70% English" in prompt
    assert "30% Italian" in prompt
    assert "one line in three" in prompt


def test_on_demands_full_italian_directive(config):
    """ON mode must demand 100% Italian dialogue and full-immersion address."""
    config.super_italian_mode = True
    prompt = _build_system_prompt(config)
    assert "100% in Italian" in prompt
    assert "amici miei" in prompt
    assert "70% English" not in prompt


def test_cache_invalidates_on_mode_flip(config):
    """Toggling the mode must rebuild the cached prompt — otherwise hot-reload lies."""
    config.super_italian_mode = False
    off_prompt = _get_system_prompt(config)
    config.super_italian_mode = True
    on_prompt = _get_system_prompt(config)
    assert off_prompt != on_prompt
    assert "amici miei" in on_prompt
    assert "amici miei" not in off_prompt
