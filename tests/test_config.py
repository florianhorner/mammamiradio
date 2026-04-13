from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.config import AudioSection, _apply_addon_options, _is_addon, load_config, runtime_json


def test_load_config_from_radio_toml(monkeypatch):
    """Loading radio.toml should produce a valid StationConfig."""
    toml_path = Path(__file__).parent.parent / "radio.toml"
    monkeypatch.delenv("STATION_NAME", raising=False)
    config = load_config(str(toml_path))

    assert config.station.name == "Mamma Mi Radio"
    assert config.station.language == "it"
    assert config.pacing.songs_between_banter == 2
    assert config.pacing.songs_between_ads == 4
    assert len(config.hosts) == 2
    assert config.hosts[0].name == "Marco"
    assert config.hosts[1].name == "Giulia"
    assert len(config.ads.brands) > 0
    assert len(config.ads.voices) > 0


def test_audio_section_loaded():
    """The [audio] section should be loaded with correct defaults."""
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))

    assert config.audio.sample_rate == 48000
    assert config.audio.channels == 2
    assert config.audio.bitrate == 192
    # CLAUDE_MODEL env override may be set; just check it's non-empty
    assert config.audio.claude_model


def test_homeassistant_section_loaded():
    """The [homeassistant] section should survive config loading."""
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))

    assert config.homeassistant.enabled is False
    assert config.homeassistant.url == ""
    assert config.homeassistant.poll_interval == 60


def test_load_config_applies_persona_arc_thresholds(tmp_path):
    """Configured arc thresholds should change the phase machine at runtime."""
    source = Path(__file__).parent.parent / "radio.toml"
    custom = source.read_text().replace(
        "arc_thresholds = [4, 11, 26]",
        "arc_thresholds = [2, 5, 9]",
    )
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    config = load_config(str(custom_path))

    from mammamiradio.persona import compute_arc_phase, set_arc_thresholds

    try:
        assert config.persona.arc_thresholds == [2, 5, 9]
        assert compute_arc_phase(2) == "acquaintance"
        assert compute_arc_phase(5) == "friend"
        assert compute_arc_phase(9) == "old_friend"
    finally:
        set_arc_thresholds([4, 11, 26])


def test_load_config_rejects_nonpositive_persona_cue_thresholds(tmp_path):
    source = Path(__file__).parent.parent / "radio.toml"
    custom = (
        source.read_text()
        .replace("anthem_threshold = 3", "anthem_threshold = 0")
        .replace(
            "skip_bit_threshold = 2",
            "skip_bit_threshold = -1",
        )
    )
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    with pytest.raises(ValueError, match="persona\\.anthem_threshold must be >= 1"):
        load_config(str(custom_path))


def test_load_config_does_not_leak_arc_thresholds_on_validation_failure(tmp_path):
    source = Path(__file__).parent.parent / "radio.toml"
    custom = (
        source.read_text()
        .replace("arc_thresholds = [4, 11, 26]", "arc_thresholds = [2, 5, 9]")
        .replace("songs_between_banter = 2", "songs_between_banter = 0")
    )
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    from mammamiradio.persona import compute_arc_phase, set_arc_thresholds

    set_arc_thresholds([4, 11, 26])
    with pytest.raises(ValueError, match="pacing\\.songs_between_banter must be >= 1"):
        load_config(str(custom_path))
    assert compute_arc_phase(5) == "acquaintance"


def test_audio_section_defaults():
    """AudioSection dataclass defaults should be sensible."""
    audio = AudioSection()
    assert audio.sample_rate == 48000
    assert audio.channels == 2
    assert audio.bitrate == 192


def test_loads_admin_env(monkeypatch):
    toml_path = Path(__file__).parent.parent / "radio.toml"
    monkeypatch.setenv("ADMIN_USERNAME", "radio")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("ADMIN_TOKEN", "token123")

    config = load_config(str(toml_path))

    assert config.admin_username == "radio"
    assert config.admin_password == "secret"
    assert config.admin_token == "token123"


def test_audio_bitrate_is_canonical():
    """audio.bitrate is the single source of truth for bitrate."""
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))
    assert config.audio.bitrate == 192
    assert not hasattr(config.station, "bitrate")


def test_runtime_json_keys():
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))
    result = runtime_json(config)
    assert "bind_host" in result
    assert "port" in result
    assert "tmp_dir" in result


def test_non_local_bind_allowed_without_auth(monkeypatch):
    """Non-local bind without auth is allowed — private networks are trusted at runtime."""
    toml_path = Path(__file__).parent.parent / "radio.toml"
    monkeypatch.setenv("MAMMAMIRADIO_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)

    config = load_config(str(toml_path))
    assert config.bind_host == "0.0.0.0"


# --- Addon detection tests ---


def test_is_addon_with_supervisor_token(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "abc123")
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    assert _is_addon() is True


def test_is_addon_with_hassio_token(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("HASSIO_TOKEN", "xyz789")
    assert _is_addon() is True


def test_is_addon_without_tokens(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    with patch("mammamiradio.config.Path.exists", return_value=False):
        assert _is_addon() is False


def test_is_addon_ignores_options_file_without_tokens(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)

    with patch("mammamiradio.config.Path.exists", return_value=True):
        assert _is_addon() is False


def test_apply_addon_options(monkeypatch, tmp_path):
    options = {"anthropic_api_key": "test_key"}
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps(options))

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    import os

    with patch("mammamiradio.config.Path") as mock_path_cls:
        mock_path_cls.return_value = options_file
        _apply_addon_options()

    assert os.environ.get("ANTHROPIC_API_KEY") == "test_key"
    # Cleanup
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_apply_addon_options_no_override(monkeypatch, tmp_path):
    """Existing env vars should not be overridden by options.json."""
    options = {"anthropic_api_key": "from_options"}
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps(options))

    monkeypatch.setenv("ANTHROPIC_API_KEY", "from_env")

    with patch("mammamiradio.config.Path") as mock_path_cls:
        mock_path_cls.return_value = options_file
        _apply_addon_options()

    import os

    assert os.environ["ANTHROPIC_API_KEY"] == "from_env"


def test_apply_addon_options_missing_file(monkeypatch):
    """No /data/options.json should be a no-op."""
    with patch("mammamiradio.config.Path") as mock_path_cls:
        mock_path_cls.return_value.exists.return_value = False
        _apply_addon_options()  # Should not raise


def test_apply_addon_options_invalid_json(monkeypatch, tmp_path):
    """Invalid JSON should be a no-op, not crash."""
    bad_file = tmp_path / "options.json"
    bad_file.write_text("not json{{{")

    with patch("mammamiradio.config.Path") as mock_path_cls:
        mock_path_cls.return_value = bad_file
        _apply_addon_options()  # Should not raise


def test_addon_mode_overrides_paths(monkeypatch):
    toml_path = Path(__file__).parent.parent / "radio.toml"
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test_token")

    config = load_config(str(toml_path))

    assert config.is_addon is True
    assert config.cache_dir == Path("/data/cache")
    assert config.tmp_dir == Path("/data/tmp")


def test_load_config_does_not_force_addon_paths_from_options_file(monkeypatch):
    toml_path = Path(__file__).parent.parent / "radio.toml"
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)

    with patch("mammamiradio.config.Path.exists", return_value=True):
        config = load_config(str(toml_path))

    assert config.is_addon is False
    assert config.cache_dir == Path("cache")
    assert config.tmp_dir == Path("tmp")


def test_addon_mode_respects_env_path_overrides(monkeypatch):
    toml_path = Path(__file__).parent.parent / "radio.toml"
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test_token")
    monkeypatch.setenv("MAMMAMIRADIO_CACHE_DIR", "/tmp/mammamiradio-data/cache")
    monkeypatch.setenv("MAMMAMIRADIO_TMP_DIR", "/tmp/mammamiradio-data/tmp")

    config = load_config(str(toml_path))

    assert config.is_addon is True
    assert config.cache_dir == Path("/tmp/mammamiradio-data/cache")
    assert config.tmp_dir == Path("/tmp/mammamiradio-data/tmp")


def test_addon_mode_auto_enables_ha(monkeypatch):
    toml_path = Path(__file__).parent.parent / "radio.toml"
    monkeypatch.setenv("SUPERVISOR_TOKEN", "my_supervisor_token")

    config = load_config(str(toml_path))

    assert config.homeassistant.enabled is True
    assert config.homeassistant.url == "http://supervisor/core"
    assert config.ha_token == "my_supervisor_token"


def test_addon_mode_skips_bind_auth(monkeypatch):
    """Addon mode should not require ADMIN_PASSWORD for non-local bind."""
    toml_path = Path(__file__).parent.parent / "radio.toml"
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test_token")
    monkeypatch.setenv("MAMMAMIRADIO_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)

    config = load_config(str(toml_path))  # Should not raise
    assert config.is_addon is True
    assert config.bind_host == "0.0.0.0"


# ---------------------------------------------------------------------------
# Campaign spine and voice role parsing
# ---------------------------------------------------------------------------


def test_load_config_parses_campaign_spines():
    """Brands with [ads.brands.campaign] sub-tables should have CampaignSpine."""
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))

    # First recurring brand should have a campaign spine
    recurring = next(b for b in config.ads.brands if b.recurring and b.campaign is not None)
    assert recurring.campaign is not None
    assert recurring.campaign.premise  # non-empty
    assert recurring.campaign.sonic_signature
    assert "classic_pitch" in recurring.campaign.format_pool
    assert recurring.campaign.spokesperson


def test_load_config_brands_without_campaign():
    """Brands without campaign sub-tables should have campaign=None."""
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))

    # Find a brand without a campaign (non-recurring brand)
    non_campaign = next(b for b in config.ads.brands if b.campaign is None)
    assert non_campaign.campaign is None


def test_load_config_parses_voice_roles():
    """Voices with role field should have it populated."""
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))

    roberto = next(v for v in config.ads.voices if v.name == "Roberto")
    assert roberto.role == "hammer"

    palmira = next(v for v in config.ads.voices if v.name == "Palmira")
    assert palmira.role == "seductress"

    # New voices
    marzio = next(v for v in config.ads.voices if v.name == "Dottore Marzio")
    assert marzio.role == "bureaucrat"
