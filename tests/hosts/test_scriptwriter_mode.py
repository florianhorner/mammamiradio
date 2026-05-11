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
    assert "ALL dialogue must be in" not in prompt
    assert "amici miei" not in prompt


def test_on_demands_full_italian_directive(config):
    """ON mode must keep the all-Italian dialogue directive and full-immersion address."""
    config.super_italian_mode = True
    prompt = _build_system_prompt(config)
    assert "ALL dialogue must be in" in prompt
    assert "amici miei" in prompt


def test_cache_invalidates_on_mode_flip(config):
    """Toggling the mode must rebuild the cached prompt — otherwise hot-reload lies."""
    config.super_italian_mode = False
    off_prompt = _get_system_prompt(config)
    config.super_italian_mode = True
    on_prompt = _get_system_prompt(config)
    assert off_prompt != on_prompt
    assert "amici miei" in on_prompt
    assert "amici miei" not in off_prompt
