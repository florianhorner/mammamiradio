"""Contracts for configuration-owned model routing and pricing."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from mammamiradio.core.config import (
    DEFAULT_ROLE,
    MODEL_REGISTRY_FILENAME,
    ModelsSection,
    _build_default_models,
    _load_model_registry,
    _parse_models_section,
    _validate_models,
    effort_for,
    load_config,
    resolve_model,
)


@pytest.fixture
def models() -> ModelsSection:
    return _build_default_models()


def test_packaged_registry_drives_every_script_route(models: ModelsSection) -> None:
    assert models.source.endswith(MODEL_REGISTRY_FILENAME)
    for profile in models.profiles:
        for provider in models.catalog:
            for caller in models.routing:
                assert resolve_model(models, caller, provider, profile=profile) in models.catalog[provider].values()


def test_direction_is_explicitly_creative(models: ModelsSection) -> None:
    assert models.routing["direction"] == DEFAULT_ROLE


def test_shipped_quality_profiles_keep_their_creative_contract(models: ModelsSection) -> None:
    expected_creative = {
        "premium": {"anthropic": "claude-opus-5", "openai": "gpt-5.6-sol"},
        "balanced": {"anthropic": "claude-sonnet-5", "openai": "gpt-5.6-terra"},
        "economy": {"anthropic": "claude-haiku-4-5-20251001", "openai": "gpt-5.6-luna"},
    }
    creative_callers = ("banter", "news_flash", "ad", "direction")
    fast_callers = ("transition", "home_mood", "memory_extract")

    for profile, expected_models in expected_creative.items():
        for provider, expected_model in expected_models.items():
            for caller in creative_callers:
                assert resolve_model(models, caller, provider, profile=profile) == expected_model

    for profile in expected_creative:
        assert {resolve_model(models, caller, "anthropic", profile=profile) for caller in fast_callers} == {
            "claude-haiku-4-5-20251001"
        }
        assert {resolve_model(models, caller, "openai", profile=profile) for caller in fast_callers} == {"gpt-5.6-luna"}


def test_unrouted_caller_uses_default_role(models: ModelsSection) -> None:
    assert resolve_model(models, "unrouted", "anthropic") == resolve_model(models, "banter", "anthropic")


def test_unknown_or_empty_provider_returns_unavailable(models: ModelsSection) -> None:
    assert resolve_model(models, "banter", "missing") is None
    models.catalog["anthropic"] = {}
    assert resolve_model(models, "banter", "anthropic") is None


def test_missing_registry_routes_to_safe_unavailable_state(tmp_path) -> None:
    models = _load_model_registry(tmp_path / MODEL_REGISTRY_FILENAME)
    assert models.source == "unavailable"
    assert resolve_model(models, "banter", "anthropic") is None
    assert models.tts_model("openai") is None


def test_malformed_registry_routes_to_safe_unavailable_state(tmp_path) -> None:
    registry_path = tmp_path / MODEL_REGISTRY_FILENAME
    registry_path.write_text("[models\nthis is not TOML", encoding="utf-8")

    models = _load_model_registry(registry_path)

    assert models.source == "unavailable"
    assert resolve_model(models, "banter", "openai") is None
    assert models.tts_model("openai") is None


def test_legacy_radio_models_are_read_only_transition_fallback(tmp_path) -> None:
    legacy = {
        "models": {
            "catalog": {"anthropic": {"creative": "legacy-model"}},
            "routing": {"banter": "creative"},
            "profiles": {"balanced": {"anthropic": {"creative": "creative"}}},
        }
    }
    models = _load_model_registry(tmp_path / MODEL_REGISTRY_FILENAME, legacy_raw=legacy)
    assert models.source == "legacy radio.toml [models]"
    assert resolve_model(models, "banter", "anthropic") == "legacy-model"
    assert models.tts_model("openai") is None


def test_partial_registry_keeps_builtin_role_routing() -> None:
    models = _parse_models_section(
        {
            "models": {
                "catalog": {"anthropic": {"creative_key": "creative-model", "fast_key": "fast-model"}},
                "profiles": {"balanced": {"anthropic": {"creative": "creative_key", "fast": "fast_key"}}},
            }
        }
    )
    assert resolve_model(models, "banter", "anthropic") == "creative-model"
    assert resolve_model(models, "transition", "anthropic") == "fast-model"


def test_invalid_inline_registry_raises_instead_of_inventing_a_model() -> None:
    with pytest.raises(ValueError):
        _parse_models_section({"models": {"catalog": "not-a-table"}})


def test_blank_catalog_value_does_not_resolve_to_a_provider_request() -> None:
    models = ModelsSection(
        catalog={"anthropic": {"creative": ""}},
        routing={"banter": "creative"},
        profiles={"balanced": {"anthropic": {"creative": "creative"}}},
    )
    assert resolve_model(models, "banter", "anthropic") is None


def test_unknown_catalog_key_does_not_resolve_to_an_arbitrary_model() -> None:
    models = ModelsSection(
        catalog={"anthropic": {"other": "other-model"}},
        routing={"banter": "creative"},
        profiles={"balanced": {"anthropic": {"creative": "missing"}}},
    )
    assert resolve_model(models, "banter", "anthropic") is None


_VALID_REGISTRY = """
[models]
default_profile = "balanced"

[models.catalog.anthropic]
opus = "anthropic-creative"
haiku = "anthropic-fast"

[models.catalog.openai]
large = "openai-creative"
small = "openai-fast"

[models.routing]
banter = "creative"
transition = "fast"

[models.profiles.balanced]
anthropic = {{ creative = "opus", fast = "haiku" }}
openai = {{ creative = "large", fast = "small" }}

[tts.openai]
model = "{tts_model}"

[pricing]
fallback_input_per_million = {fallback_input}
fallback_output_per_million = 75.0

[pricing.catalog.anthropic]
opus = {opus_price}
haiku = {{ input_per_million = 0.8, output_per_million = 4.0 }}

[pricing.catalog.openai]
large = {{ input_per_million = 5.0, output_per_million = 30.0 }}
small = {{ input_per_million = 0.75, output_per_million = 4.5 }}
"""


def _write_registry(
    tmp_path: Path,
    *,
    tts_model: str = "openai-tts",
    fallback_input: str = "15.0",
    opus_price: str = "{ input_per_million = 15.0, output_per_million = 75.0 }",
) -> Path:
    registry_path = tmp_path / MODEL_REGISTRY_FILENAME
    registry_path.write_text(
        _VALID_REGISTRY.format(tts_model=tts_model, fallback_input=fallback_input, opus_price=opus_price),
        encoding="utf-8",
    )
    return registry_path


def test_tts_typo_keeps_script_routing_and_pricing(tmp_path) -> None:
    """A blank TTS model must not strip working script routes (principle #2)."""
    models = _load_model_registry(_write_registry(tmp_path, tts_model=""))
    assert resolve_model(models, "banter", "anthropic") == "anthropic-creative"
    assert models.tts_model("openai") is None  # OpenAI TTS degrades to Edge
    assert models.price_for_model("anthropic-creative")[2] is False  # pricing still parsed


def test_pricing_typo_keeps_script_routing(tmp_path) -> None:
    """A malformed pricing field must not strip routing; cost falls back + flags."""
    # Valid TOML (a quoted string) that is not a float — fails inside pricing
    # parsing, after routing has already parsed, not at TOML load time.
    models = _load_model_registry(_write_registry(tmp_path, fallback_input='"not-a-number"'))
    assert resolve_model(models, "banter", "anthropic") == "anthropic-creative"
    assert models.tts_model("openai") == "openai-tts"  # TTS parsed before pricing
    input_rate, output_rate, unknown = models.price_for_model("anthropic-creative")
    assert input_rate > 0 and output_rate > 0
    assert unknown is True  # unpriced, not silent $0


def test_incomplete_price_entry_stays_unpriced(tmp_path) -> None:
    """A stub price table (missing rate fields) must flag unpriced, never silent $0."""
    models = _load_model_registry(_write_registry(tmp_path, opus_price="{}"))
    _, _, opus_unknown = models.price_for_model("anthropic-creative")
    _, _, haiku_unknown = models.price_for_model("anthropic-fast")
    assert opus_unknown is True  # stub entry flagged
    assert haiku_unknown is False  # sibling entry still priced


def test_non_finite_fallback_rate_keeps_conservative_default(tmp_path) -> None:
    """A nan/inf fallback rate must not reach /status; the conservative default holds."""
    import math

    models = _load_model_registry(_write_registry(tmp_path, fallback_input="nan"))
    assert resolve_model(models, "banter", "anthropic") == "anthropic-creative"  # routing intact
    input_rate, output_rate, _ = models.price_for_model("some-unpriced-model")
    assert math.isfinite(input_rate) and math.isfinite(output_rate)
    assert input_rate > 0 and output_rate > 0  # conservative fallback, not nan


def test_non_finite_catalog_rate_stays_unpriced(tmp_path) -> None:
    """An inf catalog rate flags the model unpriced (never serializes a bad float)."""
    import math

    models = _load_model_registry(
        _write_registry(tmp_path, opus_price="{ input_per_million = inf, output_per_million = 75.0 }")
    )
    opus_in, opus_out, opus_unknown = models.price_for_model("anthropic-creative")
    _, _, haiku_unknown = models.price_for_model("anthropic-fast")
    assert opus_unknown is True  # non-finite entry dropped to unpriced
    assert math.isfinite(opus_in) and math.isfinite(opus_out)  # fallback, finite
    assert haiku_unknown is False  # sibling entry still priced


def test_invalid_utf8_registry_falls_back_without_aborting(tmp_path) -> None:
    """A corrupt (non-UTF-8) registry must degrade to unavailable, never crash boot."""
    registry_path = tmp_path / MODEL_REGISTRY_FILENAME
    registry_path.write_bytes(b"[models]\nkey = \xff\xfe\x00\n")
    models = _load_model_registry(registry_path)
    assert models.source == "unavailable"
    assert resolve_model(models, "banter", "anthropic") is None


def test_registry_pricing_and_unknown_fallback_are_exposed(models: ModelsSection) -> None:
    priced = models.default_openai_eval_models()
    assert priced == list(dict.fromkeys(models.catalog["openai"].values()))
    assert all(models.price_for_model(model)[2] is False for model in priced)
    input_rate, output_rate, unknown = models.price_for_model("not-in-registry")
    assert input_rate > 0 and output_rate > 0 and unknown is True


def test_runtime_source_and_eval_defaults_do_not_embed_registry_model_ids() -> None:
    root = Path(__file__).resolve().parents[2]
    with (root / MODEL_REGISTRY_FILENAME).open("rb") as registry_file:
        registry = tomllib.load(registry_file)
    ids = [
        model_id
        for provider_catalog in registry["models"]["catalog"].values()
        for model_id in provider_catalog.values()
    ] + [registry["tts"]["openai"]["model"]]
    source_paths = [*sorted((root / "mammamiradio").rglob("*.py")), root / "scripts" / "eval_openai_script_model.py"]
    for source_path in source_paths:
        text = source_path.read_text(encoding="utf-8")
        for model_id in ids:
            assert model_id not in text, f"{source_path.relative_to(root)} embeds {model_id}; use model_registry.toml"


def test_validate_models_does_not_reintroduce_a_code_catalog(monkeypatch) -> None:
    from mammamiradio.core.config import StationConfig

    cfg = StationConfig.__new__(StationConfig)
    cfg.anthropic_api_key = "key"
    cfg.openai_api_key = ""
    cfg.models = ModelsSection(
        catalog={"anthropic": {"creative": ""}},
        routing={"banter": "creative"},
        profiles={"balanced": {"anthropic": {"creative": "creative"}}},
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _validate_models(cfg)
    assert resolve_model(cfg.models, "banter", "anthropic") is None


def test_quality_env_selects_a_registry_profile(monkeypatch) -> None:
    monkeypatch.setenv("MAMMAMIRADIO_QUALITY", "economy")
    assert load_config().models.active_profile == "economy"


def test_quality_env_ignores_unknown_profile(monkeypatch) -> None:
    monkeypatch.setenv("MAMMAMIRADIO_QUALITY", "missing")
    assert load_config().models.active_profile == "balanced"


def test_shipped_registry_parses_effort_for_creative_only(models: ModelsSection) -> None:
    assert models.effort["anthropic"] == {"opus": "medium", "sonnet": "medium"}
    assert "haiku" not in models.effort["anthropic"]
    assert effort_for(models, "anthropic", "claude-opus-5", caller="banter", profile="premium") == "medium"
    assert effort_for(models, "anthropic", "claude-sonnet-5", caller="banter") == "medium"
    assert effort_for(models, "anthropic", "claude-haiku-4-5-20251001", caller="transition") is None
    assert effort_for(models, "anthropic", "claude-fable-5-1", caller="banter") is None
    assert effort_for(models, "openai", "gpt-5.6-sol", caller="banter", profile="premium") is None


def test_memory_extraction_never_carries_effort(models: ModelsSection) -> None:
    """Post-air memory extraction shares the fast role and must stay effort-free."""
    from mammamiradio.hosts.memory_extractor import MEMORY_EXTRACT_CALLER

    model = resolve_model(models, MEMORY_EXTRACT_CALLER, "anthropic")

    assert model == "claude-haiku-4-5-20251001"
    assert effort_for(models, "anthropic", model, caller=MEMORY_EXTRACT_CALLER) is None


def test_effort_follows_default_profile_when_active_profile_omits_provider() -> None:
    """effort_for resolves through the same profile fallback as resolve_model."""
    models = _parse_models_section(
        {
            "models": {
                "default_profile": "balanced",
                "catalog": {"anthropic": {"sonnet": "claude-sonnet-5"}},
                "profiles": {
                    "balanced": {"anthropic": {"creative": "sonnet"}},
                    "economy": {"openai": {"creative": "small"}},
                },
                "effort": {"anthropic": {"sonnet": "medium"}},
            }
        }
    )

    assert resolve_model(models, "banter", "anthropic", profile="economy") == "claude-sonnet-5"
    assert effort_for(models, "anthropic", "claude-sonnet-5", caller="banter", profile="economy") == "medium"


def test_fast_env_override_matching_creative_model_does_not_inherit_effort(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5")
    models = load_config().models

    fast_model = resolve_model(models, "transition", "anthropic")

    assert fast_model == "claude-sonnet-5"
    assert effort_for(models, "anthropic", fast_model, caller="transition") is None


def test_effort_parse_drops_invalid_level_and_unknown_key(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        models = _parse_models_section(
            {
                "models": {
                    "catalog": {
                        "anthropic": {"opus": "claude-opus-5", "haiku": "claude-haiku-4-5-20251001"},
                    },
                    "profiles": {"balanced": {"anthropic": {"creative": "opus", "fast": "haiku"}}},
                    "effort": {
                        "anthropic": {
                            "opus": "turbo",
                            "missing": "medium",
                            "haiku": "low",
                        }
                    },
                }
            }
        )
    assert models.effort == {}
    joined = " ".join(record.message for record in caplog.records)
    assert "invalid level" in joined
    assert "not in models.catalog" in joined
    assert "Haiku" in joined
    assert effort_for(models, "anthropic", "claude-opus-5", caller="banter", profile="premium") is None


def test_effort_absent_table_is_empty() -> None:
    models = _parse_models_section(
        {
            "models": {
                "catalog": {"anthropic": {"opus": "claude-opus-5"}},
                "profiles": {"balanced": {"anthropic": {"creative": "opus"}}},
            }
        }
    )
    assert models.effort == {}
    assert effort_for(models, "anthropic", "claude-opus-5", caller="banter", profile="premium") is None
