"""Tests for dynamic LLM routing: task→role→profile→catalog resolution, the
config-derived floor, env overrides, and degrade-don't-die validation.

The contract these lock:
  * resolve_model is TOTAL — it never raises and always returns a non-empty,
    config-derived model ID (a crash here = dead air, leadership principle #1).
  * a malformed/incomplete [models] block DEGRADES to the built-in defaults so
    the station always boots and airs (principle #2), it never fails boot.
  * `fast` is pinned to the lowest-latency model in every profile.
"""

from __future__ import annotations

import pytest

from mammamiradio.core.config import (
    DEFAULT_ROLE,
    ModelsSection,
    _build_default_models,
    _parse_models_section,
    _validate_models,
    load_config,
    resolve_model,
)

CALLERS = ["banter", "news_flash", "ad", "transition"]


@pytest.fixture
def models() -> ModelsSection:
    return _build_default_models()


# ── Resolution: task → role → model, per profile ──────────────────────────
def test_balanced_preserves_prior_behavior(models):
    """Balanced reproduces the pre-refactor mapping: creative=opus, fast=haiku."""
    models.active_profile = "balanced"
    assert resolve_model(models, "banter", "anthropic") == "claude-opus-4-8"
    assert resolve_model(models, "news_flash", "anthropic") == "claude-opus-4-8"
    assert resolve_model(models, "ad", "anthropic") == "claude-opus-4-8"
    assert resolve_model(models, "transition", "anthropic") == "claude-haiku-4-5-20251001"


def test_profile_switch_changes_resolved_model(models):
    models.active_profile = "economy"
    assert resolve_model(models, "banter", "anthropic") == "claude-haiku-4-5-20251001"
    models.active_profile = "premium"
    assert resolve_model(models, "banter", "anthropic") == "claude-opus-4-8"


def test_fast_role_is_low_latency_in_every_profile(models):
    """Transitions (fast role) must never get a slow model — dead-air risk."""
    for profile in models.profiles:
        models.active_profile = profile
        assert resolve_model(models, "transition", "anthropic") == "claude-haiku-4-5-20251001"


def test_openai_fallback_resolves_same_role(models):
    models.active_profile = "premium"
    # premium openai: creative=large(gpt-4o), fast=small(gpt-4o-mini)
    assert resolve_model(models, "banter", "openai") == "gpt-4o"
    assert resolve_model(models, "transition", "openai") == "gpt-4o-mini"


def test_explicit_profile_arg_overrides_active(models):
    models.active_profile = "economy"
    assert resolve_model(models, "banter", "anthropic", profile="premium") == "claude-opus-4-8"


# ── Floor / never-raises ──────────────────────────────────────────────────
def test_unrouted_caller_uses_default_role(models):
    assert DEFAULT_ROLE == "creative"
    # an unknown task falls to the creative role, not a crash
    assert resolve_model(models, "totally_new_task", "anthropic") == resolve_model(models, "banter", "anthropic")


def test_resolve_never_raises_on_missing_profile(models):
    models.active_profile = "nonexistent"
    # falls back to default_profile resolution
    assert resolve_model(models, "banter", "anthropic") == "claude-opus-4-8"


def test_resolve_never_raises_on_missing_catalog_key(models):
    # break the key the active profile points at; floor must still return a real id
    models.profiles["balanced"]["anthropic"]["creative"] = "does-not-exist"
    out = resolve_model(models, "banter", "anthropic")
    assert out and out in models.catalog["anthropic"].values()


def test_resolve_never_raises_on_unknown_provider(models):
    out = resolve_model(models, "banter", "nonexistent_provider")
    assert isinstance(out, str) and out  # built-in last-resort, non-empty


def test_resolve_never_raises_when_both_profiles_missing(models):
    """Operator deletes a profile AND forgets to fix default_profile — still total."""
    models.active_profile = "nonexistent"
    models.default_profile = "also_gone"
    out = resolve_model(models, "banter", "anthropic")
    assert out and isinstance(out, str)


def test_floor_is_named_not_dict_ordered(models):
    """When the provider catalog is emptied, the floor pins to a named low-cost
    model (haiku/small), never the first dict entry — ordering must not leak."""
    models.catalog["anthropic"] = {}  # force the built-in last-resort path
    assert resolve_model(models, "banter", "anthropic") == "claude-haiku-4-5-20251001"


def test_resolve_handles_none_caller(models):
    assert resolve_model(models, None, "anthropic")  # provider probe path


# ── Degrade-don't-die parsing/validation ──────────────────────────────────
def test_no_models_block_uses_defaults():
    m = _parse_models_section({})
    assert m.catalog["anthropic"]["opus"] == "claude-opus-4-8"


def test_malformed_models_block_degrades():
    m = _parse_models_section({"models": {"catalog": "not-a-table"}})
    assert set(m.profiles) == {"premium", "balanced", "economy"}


def test_empty_catalog_degrades():
    m = _parse_models_section({"models": {"catalog": {}, "profiles": {}}})
    assert m.catalog  # non-empty after degrade


def test_validate_models_degrades_on_unresolved_role(monkeypatch):
    """A keyed provider whose active profile can't resolve a routed role must
    degrade to DEFAULT_MODELS, not stop the station."""
    from mammamiradio.core.config import StationConfig

    cfg = StationConfig.__new__(StationConfig)
    cfg.anthropic_api_key = "sk-ant"
    cfg.openai_api_key = ""
    # catalog has no entry for the key the profile points at
    cfg.models = ModelsSection(
        catalog={"anthropic": {"x": "model-x"}},
        routing={"banter": "creative"},
        profiles={"balanced": {"anthropic": {"creative": "missing_key"}}},
        default_profile="balanced",
        active_profile="balanced",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _validate_models(cfg)
    # degraded back to a working built-in catalog
    assert resolve_model(cfg.models, "banter", "anthropic")
    assert cfg.models.catalog["anthropic"]["opus"] == "claude-opus-4-8"


def test_validate_models_noop_without_keys():
    from mammamiradio.core.config import StationConfig

    cfg = StationConfig.__new__(StationConfig)
    cfg.anthropic_api_key = ""
    cfg.openai_api_key = ""
    broken = ModelsSection(catalog={}, routing={}, profiles={}, default_profile="x", active_profile="x")
    cfg.models = broken
    _validate_models(cfg)  # no providers → nothing to validate, no degrade
    assert cfg.models is broken


# ── Env-driven active profile ─────────────────────────────────────────────
def test_quality_env_sets_active_profile(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_QUALITY", "economy")
    cfg = load_config()
    assert cfg.models.active_profile == "economy"


def test_quality_env_ignores_unknown_profile(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_QUALITY", "ultra-premium-bogus")
    cfg = load_config()
    assert cfg.models.active_profile == "balanced"  # default preserved
