from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from mammamiradio.config import AudioSection, _apply_addon_options, _is_addon, load_config, runtime_json


def test_load_config_from_radio_toml():
    """Loading radio.toml should produce a valid StationConfig."""
    # Ensure we load from the project root radio.toml
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))

    assert config.station.name == "Malamie Radio"
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
    assert config.audio.spotify_bitrate == 320
    assert config.audio.fifo_path == "/tmp/mammamiradio.pcm"
    assert "go-librespot" in config.audio.go_librespot_bin
    assert config.audio.go_librespot_port == 3678
    # CLAUDE_MODEL env override may be set; just check it's non-empty
    assert config.audio.claude_model


def test_homeassistant_section_loaded():
    """The [homeassistant] section should survive config loading."""
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))

    assert config.homeassistant.enabled is False
    assert config.homeassistant.url == ""
    assert config.homeassistant.poll_interval == 60


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
    assert "fifo_path" in result
    assert "go_librespot_bin" in result
    assert "go_librespot_config_dir" in result


def test_runtime_json_includes_resolved_go_librespot_config_dir():
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))
    config.audio.go_librespot_config_dir = "custom-go-librespot"

    result = runtime_json(config)

    assert result["go_librespot_config_dir"] == "custom-go-librespot"


def test_non_local_bind_requires_admin_auth(monkeypatch):
    toml_path = Path(__file__).parent.parent / "radio.toml"
    monkeypatch.setenv("MAMMAMIRADIO_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)

    try:
        load_config(str(toml_path))
    except ValueError as exc:
        assert "Non-local bind requires ADMIN_PASSWORD or ADMIN_TOKEN" in str(exc)
    else:
        raise AssertionError("Expected config validation to fail for non-local bind without auth")


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
    assert _is_addon() is False


def test_apply_addon_options(monkeypatch, tmp_path):
    options = {"spotify_client_id": "test_id", "spotify_client_secret": "test_secret"}
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps(options))

    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)

    import os

    with patch("mammamiradio.config.Path") as mock_path_cls:
        mock_path_cls.return_value = options_file
        _apply_addon_options()

    assert os.environ.get("SPOTIFY_CLIENT_ID") == "test_id"
    assert os.environ.get("SPOTIFY_CLIENT_SECRET") == "test_secret"
    # Cleanup
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)


def test_apply_addon_options_no_override(monkeypatch, tmp_path):
    """Existing env vars should not be overridden by options.json."""
    options = {"spotify_client_id": "from_options"}
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps(options))

    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "from_env")

    with patch("mammamiradio.config.Path") as mock_path_cls:
        mock_path_cls.return_value = options_file
        _apply_addon_options()

    import os

    assert os.environ["SPOTIFY_CLIENT_ID"] == "from_env"


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
    assert config.audio.go_librespot_config_dir == "/data/go-librespot"


def test_addon_mode_respects_env_path_overrides(monkeypatch):
    toml_path = Path(__file__).parent.parent / "radio.toml"
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test_token")
    monkeypatch.setenv("MAMMAMIRADIO_CACHE_DIR", "/tmp/mammamiradio-data/cache")
    monkeypatch.setenv("MAMMAMIRADIO_TMP_DIR", "/tmp/mammamiradio-data/tmp")
    monkeypatch.setenv("MAMMAMIRADIO_GO_LIBRESPOT_CONFIG_DIR", "/tmp/mammamiradio-data/go-librespot")

    config = load_config(str(toml_path))

    assert config.is_addon is True
    assert config.cache_dir == Path("/tmp/mammamiradio-data/cache")
    assert config.tmp_dir == Path("/tmp/mammamiradio-data/tmp")
    assert config.audio.go_librespot_config_dir == "/tmp/mammamiradio-data/go-librespot"


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

    # Negroni as a Service has a campaign spine
    negroni = next(b for b in config.ads.brands if b.name == "Negroni as a Service")
    assert negroni.campaign is not None
    assert "cloud" in negroni.campaign.premise.lower()
    assert negroni.campaign.sonic_signature == "ice_clink+startup_synth"
    assert "classic_pitch" in negroni.campaign.format_pool
    assert negroni.campaign.spokesperson == "hammer"


def test_load_config_brands_without_campaign():
    """Brands without campaign sub-tables should have campaign=None."""
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))

    # Mausoleo Berlusconi has no campaign
    mausoleo = next(b for b in config.ads.brands if b.name == "Mausoleo Berlusconi")
    assert mausoleo.campaign is None


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
