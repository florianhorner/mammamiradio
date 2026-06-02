from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.core.config import (
    AudioSection,
    _apply_addon_options,
    _err,
    _is_addon,
    _validate,
    coerce_bool,
    load_config,
    runtime_json,
)


def test_load_config_from_radio_toml(monkeypatch):
    """Loading radio.toml should produce a valid StationConfig."""
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    monkeypatch.delenv("STATION_NAME", raising=False)
    config = load_config(str(toml_path))

    assert config.station.name == "Mamma Mi Radio"
    assert config.station.language == "it"
    assert config.pacing.songs_between_banter == 2
    assert config.pacing.songs_between_ads == 4
    assert config.super_italian_mode is True
    assert len(config.hosts) == 2
    assert config.hosts[0].name == "Marco"
    assert config.hosts[1].name == "Giulia"
    assert len(config.ads.brands) > 0
    assert len(config.ads.voices) > 0


def test_load_config_sets_default_edge_fallback_for_openai_hosts(tmp_path):
    source = Path(__file__).resolve().parents[2] / "radio.toml"
    custom = source.read_text().replace('edge_fallback_voice = "it-IT-GiuseppeMultilingualNeural"\n', "")
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    config = load_config(str(custom_path))

    marco = next(h for h in config.hosts if h.name == "Marco")
    assert marco.engine == "openai"
    assert marco.edge_fallback_voice == "it-IT-DiegoNeural"


def test_load_config_normalizes_edge_host_with_openai_voice(tmp_path):
    source = Path(__file__).resolve().parents[2] / "radio.toml"
    custom = source.read_text().replace(
        'voice = "onyx"\nengine = "openai"\nedge_fallback_voice = "it-IT-GiuseppeMultilingualNeural"',
        'voice = "onyx"\nengine = "edge"\nedge_fallback_voice = ""',
    )
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    config = load_config(str(custom_path))

    marco = next(h for h in config.hosts if h.name == "Marco")
    assert marco.engine == "edge"
    assert marco.voice == "it-IT-DiegoNeural"


def test_load_config_normalizes_ad_voice_with_openai_id(tmp_path):
    source = Path(__file__).resolve().parents[2] / "radio.toml"
    custom = source.read_text().replace(
        'name = "Roberto"\nvoice = "it-IT-DiegoNeural"',
        'name = "Roberto"\nvoice = "onyx"',
    )
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    config = load_config(str(custom_path))

    roberto = next(v for v in config.ads.voices if v.name == "Roberto")
    assert roberto.voice == "it-IT-DiegoNeural"


def test_audio_section_loaded():
    """The [audio] section should be loaded with correct defaults."""
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    config = load_config(str(toml_path))

    assert config.audio.sample_rate == 48000
    assert config.audio.channels == 2
    assert config.audio.bitrate == 192
    # CLAUDE_MODEL env override may be set; just check it's non-empty
    assert config.audio.claude_model


def test_homeassistant_section_loaded():
    """The [homeassistant] section should survive config loading."""
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    config = load_config(str(toml_path))

    assert config.homeassistant.enabled is False
    assert config.homeassistant.url == ""
    assert config.homeassistant.poll_interval == 60


def test_load_config_parses_homeassistant_timer_interrupts(tmp_path):
    source = Path(__file__).resolve().parents[2] / "radio.toml"
    custom = (
        source.read_text()
        + """

[[homeassistant.timer_interrupt]]
entity_id = "timer.pasta_timer"
directive = "La pasta e pronta!"
urgency = "urgent"
cooldown = 300

[[homeassistant.timer_interrupt]]
entity_id = "timer.lavatrice"
directive = "Lavatrice finita!"
"""
    )
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    config = load_config(str(custom_path))

    assert len(config.homeassistant.timer_interrupts) == 2
    explicit = config.homeassistant.timer_interrupts[0]
    assert explicit.entity_id == "timer.pasta_timer"
    assert explicit.directive == "La pasta e pronta!"
    assert explicit.urgency == "urgent"
    assert explicit.cooldown == 300
    # Defaults apply when urgency / cooldown are omitted.
    defaults = config.homeassistant.timer_interrupts[1]
    assert defaults.entity_id == "timer.lavatrice"
    assert defaults.directive == "Lavatrice finita!"
    assert defaults.urgency == "pissed"
    assert defaults.cooldown == 60


def test_load_config_applies_persona_arc_thresholds(tmp_path):
    """Configured arc thresholds should change the phase machine at runtime."""
    source = Path(__file__).resolve().parents[2] / "radio.toml"
    custom = source.read_text().replace(
        "arc_thresholds = [4, 11, 26]",
        "arc_thresholds = [2, 5, 9]",
    )
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    config = load_config(str(custom_path))

    from mammamiradio.hosts.persona import compute_arc_phase, set_arc_thresholds

    try:
        assert config.persona.arc_thresholds == [2, 5, 9]
        assert compute_arc_phase(2) == "acquaintance"
        assert compute_arc_phase(5) == "friend"
        assert compute_arc_phase(9) == "old_friend"
    finally:
        set_arc_thresholds([4, 11, 26])


def test_load_config_rejects_nonpositive_persona_cue_thresholds(tmp_path):
    source = Path(__file__).resolve().parents[2] / "radio.toml"
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
    source = Path(__file__).resolve().parents[2] / "radio.toml"
    custom = (
        source.read_text()
        .replace("arc_thresholds = [4, 11, 26]", "arc_thresholds = [2, 5, 9]")
        .replace("songs_between_banter = 2", "songs_between_banter = 0")
    )
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    from mammamiradio.hosts.persona import compute_arc_phase, set_arc_thresholds

    set_arc_thresholds([4, 11, 26])
    with pytest.raises(ValueError, match="pacing\\.songs_between_banter must be >= 2"):
        load_config(str(custom_path))
    assert compute_arc_phase(5) == "acquaintance"


def test_load_config_rejects_every_song_banter_cadence(tmp_path):
    source = Path(__file__).resolve().parents[2] / "radio.toml"
    custom = source.read_text().replace("songs_between_banter = 2", "songs_between_banter = 1")
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    with pytest.raises(ValueError, match="pacing\\.songs_between_banter must be >= 2"):
        load_config(str(custom_path))


def test_load_config_rejects_pacing_above_safe_ceiling(tmp_path):
    """Config-load enforces the same ceilings as PATCH /api/pacing — a stray
    radio.toml cannot disable banter/ads by going past the runtime clamp."""
    source = Path(__file__).resolve().parents[2] / "radio.toml"
    custom = source.read_text().replace("songs_between_banter = 2", "songs_between_banter = 999")
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    with pytest.raises(ValueError, match="pacing\\.songs_between_banter must be <= 60"):
        load_config(str(custom_path))


@pytest.mark.parametrize(
    ("replace", "with_", "match"),
    [
        ("songs_between_ads = 4", "songs_between_ads = 999", "songs_between_ads must be <= 60"),
        ("ad_spots_per_break = 2", "ad_spots_per_break = 0", "ad_spots_per_break must be >= 1"),
        ("ad_spots_per_break = 2", "ad_spots_per_break = 99", "ad_spots_per_break must be <= 5"),
    ],
)
def test_load_config_rejects_pacing_out_of_range(tmp_path, replace, with_, match):
    """Every pacing floor/ceiling is enforced at config load, mirroring PATCH."""
    source = Path(__file__).resolve().parents[2] / "radio.toml"
    custom = source.read_text().replace(replace, with_)
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    with pytest.raises(ValueError, match="pacing\\." + match):
        load_config(str(custom_path))


def test_audio_section_defaults():
    """AudioSection dataclass defaults should be sensible."""
    audio = AudioSection()
    assert audio.sample_rate == 48000
    assert audio.channels == 2
    assert audio.bitrate == 192


def test_loads_admin_env(monkeypatch):
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    monkeypatch.setenv("ADMIN_USERNAME", "radio")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("ADMIN_TOKEN", "token123")

    config = load_config(str(toml_path))

    assert config.admin_username == "radio"
    assert config.admin_password == "secret"
    assert config.admin_token == "token123"


def test_audio_bitrate_is_canonical():
    """audio.bitrate is the single source of truth for bitrate."""
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    config = load_config(str(toml_path))
    assert config.audio.bitrate == 192
    assert not hasattr(config.station, "bitrate")


def test_runtime_json_keys():
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    config = load_config(str(toml_path))
    result = runtime_json(config)
    assert "bind_host" in result
    assert "port" in result
    assert "tmp_dir" in result


def test_non_local_bind_requires_auth(monkeypatch):
    """Non-loopback bind without ADMIN_PASSWORD/ADMIN_TOKEN must be rejected."""
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    monkeypatch.setenv("MAMMAMIRADIO_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)

    with pytest.raises(ValueError, match="ADMIN_PASSWORD or ADMIN_TOKEN"):
        load_config(str(toml_path))


def test_non_local_bind_allowed_with_token(monkeypatch):
    """Non-loopback bind is allowed once an admin credential is configured."""
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    monkeypatch.setenv("MAMMAMIRADIO_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("ADMIN_TOKEN", "tok-non-local")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    config = load_config(str(toml_path))
    assert config.bind_host == "0.0.0.0"


def test_empty_bind_host_requires_auth(monkeypatch):
    """Empty bind host binds all interfaces, so it needs admin creds too."""
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    monkeypatch.setenv("MAMMAMIRADIO_BIND_HOST", "")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)

    with pytest.raises(ValueError, match="ADMIN_PASSWORD or ADMIN_TOKEN"):
        load_config(str(toml_path))


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
    with patch("mammamiradio.core.config.Path.exists", return_value=False):
        assert _is_addon() is False


def test_is_addon_ignores_options_file_without_tokens(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)

    with patch("mammamiradio.core.config.Path.exists", return_value=True):
        assert _is_addon() is False


def test_apply_addon_options(monkeypatch, tmp_path):
    options = {"anthropic_api_key": "test_key"}
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps(options))

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    import os

    with patch("mammamiradio.core.config.Path") as mock_path_cls:
        mock_path_cls.return_value = options_file
        _apply_addon_options()

    assert os.environ.get("ANTHROPIC_API_KEY") == "test_key"
    # Cleanup
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.mark.parametrize(("value", "expected_env"), [(True, "true"), (False, "false")])
def test_apply_addon_options_super_italian_round_trips(monkeypatch, tmp_path, value, expected_env):
    """Addon options.json super_italian_mode bool should populate the env var."""
    import os

    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps({"super_italian_mode": value}))
    monkeypatch.delenv("MAMMAMIRADIO_SUPER_ITALIAN", raising=False)
    try:
        with patch("mammamiradio.core.config.Path") as mock_path_cls:
            mock_path_cls.return_value = options_file
            _apply_addon_options()
        assert os.environ.get("MAMMAMIRADIO_SUPER_ITALIAN") == expected_env
    finally:
        os.environ.pop("MAMMAMIRADIO_SUPER_ITALIAN", None)


def test_apply_addon_options_super_italian_preset_env_wins(monkeypatch, tmp_path):
    """If MAMMAMIRADIO_SUPER_ITALIAN is already set, options.json must NOT override."""
    import os

    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps({"super_italian_mode": True}))
    monkeypatch.setenv("MAMMAMIRADIO_SUPER_ITALIAN", "false")
    try:
        with patch("mammamiradio.core.config.Path") as mock_path_cls:
            mock_path_cls.return_value = options_file
            _apply_addon_options()
        # Pre-set env var preserved despite options.json saying True
        assert os.environ["MAMMAMIRADIO_SUPER_ITALIAN"] == "false"
    finally:
        os.environ.pop("MAMMAMIRADIO_SUPER_ITALIAN", None)


def test_apply_addon_options_no_override(monkeypatch, tmp_path):
    """Existing env vars should not be overridden by options.json."""
    options = {"anthropic_api_key": "from_options"}
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps(options))

    monkeypatch.setenv("ANTHROPIC_API_KEY", "from_env")

    with patch("mammamiradio.core.config.Path") as mock_path_cls:
        mock_path_cls.return_value = options_file
        _apply_addon_options()

    import os

    assert os.environ["ANTHROPIC_API_KEY"] == "from_env"


def test_apply_addon_options_missing_file(monkeypatch):
    """No /data/options.json should be a no-op."""
    with patch("mammamiradio.core.config.Path") as mock_path_cls:
        mock_path_cls.return_value.exists.return_value = False
        _apply_addon_options()  # Should not raise


def test_apply_addon_options_invalid_json(monkeypatch, tmp_path):
    """Invalid JSON should be a no-op, not crash."""
    bad_file = tmp_path / "options.json"
    bad_file.write_text("not json{{{")

    with patch("mammamiradio.core.config.Path") as mock_path_cls:
        mock_path_cls.return_value = bad_file
        _apply_addon_options()  # Should not raise


def test_addon_mode_overrides_paths(monkeypatch):
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test_token")

    config = load_config(str(toml_path))

    assert config.is_addon is True
    assert config.cache_dir == Path("/data/cache")
    assert config.tmp_dir == Path("/data/tmp")


def test_load_config_does_not_force_addon_paths_from_options_file(monkeypatch):
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)

    with patch("mammamiradio.core.config.Path.exists", return_value=True):
        config = load_config(str(toml_path))

    assert config.is_addon is False
    assert config.cache_dir == Path("cache")
    assert config.tmp_dir == Path("tmp")


def test_addon_mode_respects_env_path_overrides(monkeypatch):
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test_token")
    monkeypatch.setenv("MAMMAMIRADIO_CACHE_DIR", "/tmp/mammamiradio-data/cache")
    monkeypatch.setenv("MAMMAMIRADIO_TMP_DIR", "/tmp/mammamiradio-data/tmp")

    config = load_config(str(toml_path))

    assert config.is_addon is True
    assert config.cache_dir == Path("/tmp/mammamiradio-data/cache")
    assert config.tmp_dir == Path("/tmp/mammamiradio-data/tmp")


def test_addon_mode_auto_enables_ha(monkeypatch):
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    monkeypatch.setenv("SUPERVISOR_TOKEN", "my_supervisor_token")

    config = load_config(str(toml_path))

    assert config.homeassistant.enabled is True
    assert config.homeassistant.url == "http://supervisor/core"
    assert config.ha_token == "my_supervisor_token"


def test_addon_mode_respects_ha_enabled_false(monkeypatch):
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    monkeypatch.setenv("SUPERVISOR_TOKEN", "my_supervisor_token")
    monkeypatch.setenv("HA_ENABLED", "false")

    config = load_config(str(toml_path))

    assert config.is_addon is True
    assert config.homeassistant.enabled is False
    assert config.ha_token == ""


def test_addon_mode_bind_auth_uses_supervisor_token(monkeypatch):
    """Addon mode binds 0.0.0.0 but run.sh auto-generates ADMIN_TOKEN first,
    so config validation passes via the standard credential requirement."""
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test_token")
    monkeypatch.setenv("MAMMAMIRADIO_BIND_HOST", "0.0.0.0")
    # rootfs/run.sh exports ADMIN_TOKEN before launching the app in addon mode.
    monkeypatch.setenv("ADMIN_TOKEN", "addon-generated-token")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    config = load_config(str(toml_path))  # Should not raise
    assert config.is_addon is True
    assert config.bind_host == "0.0.0.0"


# ---------------------------------------------------------------------------
# Campaign spine and voice role parsing
# ---------------------------------------------------------------------------


def test_load_config_parses_campaign_spines():
    """Brands with [ads.brands.campaign] sub-tables should have CampaignSpine."""
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
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
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    config = load_config(str(toml_path))

    # Find a brand without a campaign (non-recurring brand)
    non_campaign = next(b for b in config.ads.brands if b.campaign is None)
    assert non_campaign.campaign is None


def test_load_config_parses_voice_roles():
    """Voices with role field should have it populated."""
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    config = load_config(str(toml_path))

    roberto = next(v for v in config.ads.voices if v.name == "Roberto")
    assert roberto.role == "hammer"

    palmira = next(v for v in config.ads.voices if v.name == "Palmira")
    assert palmira.role == "seductress"

    # New voices
    marzio = next(v for v in config.ads.voices if v.name == "Dottore Marzio")
    assert marzio.role == "bureaucrat"


def test_load_config_rejects_nonpositive_pacing_values(tmp_path):
    """_validate raises ValueError when pacing.songs_between_ads < 1."""
    source = Path(__file__).resolve().parents[2] / "radio.toml"
    custom = source.read_text().replace("songs_between_ads = 4", "songs_between_ads = 0")
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    with pytest.raises(ValueError, match="pacing\\.songs_between_ads must be >= 1"):
        load_config(str(custom_path))


def test_load_config_rejects_zero_timer_poll_interval(tmp_path):
    source = Path(__file__).resolve().parents[2] / "radio.toml"
    custom = source.read_text().replace("timer_poll_interval = 5", "timer_poll_interval = 0")
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    with pytest.raises(ValueError, match="homeassistant\\.timer_poll_interval must be >= 1"):
        load_config(str(custom_path))


def test_load_config_rejects_invalid_timer_interrupt_urgency_and_cooldown(tmp_path):
    source = Path(__file__).resolve().parents[2] / "radio.toml"
    custom = (
        source.read_text()
        + """

[[homeassistant.timer_interrupt]]
entity_id = "timer.pasta_timer"
directive = "Bad config!"
urgency = "screaming"
cooldown = 0
"""
    )
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    with pytest.raises(ValueError) as exc:
        load_config(str(custom_path))
    msg = str(exc.value)
    assert "homeassistant.timer_interrupt[0].cooldown must be >= 1" in msg
    assert "homeassistant.timer_interrupt[0].urgency must be one of" in msg


def test_load_config_tolerates_legacy_sonic_brand_keys(tmp_path):
    """load_config must not crash on legacy [sonic_brand] keys from older operator configs.

    Without the pop() shim in load_config(), SonicBrandSection(**raw) would raise
    TypeError on the unknown kwargs — Python @dataclass __init__ rejects extras.
    This guards both the shim and the deletion of the dataclass fields.
    """
    from dataclasses import fields as _dc_fields

    from mammamiradio.core.config import SonicBrandSection

    # Sanity: the dataclass must NOT carry the legacy fields (proves they were deleted).
    field_names = {f.name for f in _dc_fields(SonicBrandSection)}
    assert "short_sting" not in field_names
    assert "sweeper_probability" not in field_names

    # Without the shim, SonicBrandSection(**{"short_sting": ...}) raises TypeError.
    # Confirm the failure mode the shim defends against actually exists.
    with pytest.raises(TypeError):
        SonicBrandSection(short_sting="legacy")  # type: ignore[call-arg]

    source = Path(__file__).resolve().parents[2] / "radio.toml"
    raw = source.read_text()
    anchor = 'sweeper_voice = "it-IT-GiuseppeMultilingualNeural"'
    assert anchor in raw, "anchor line drifted; update this test's injection point"
    custom = raw.replace(
        anchor,
        f'{anchor}\nshort_sting = "Malamie..."\nsweeper_probability = 0.25',
    )
    # Guard the str.replace from silently no-op'ing if radio.toml ever drifts.
    assert 'short_sting = "Malamie..."' in custom
    assert "sweeper_probability = 0.25" in custom
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    config = load_config(str(custom_path))  # must not raise — shim swallows legacy keys

    assert not hasattr(config.sonic_brand, "short_sting")
    assert not hasattr(config.sonic_brand, "sweeper_probability")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("True", True),
        ("YES", True),
        ("1", True),
        ("false", False),  # the load-bearing case: bool("false") would be True
        ("False", False),
        ("no", False),
        ("0", False),
        (1, True),
        (0, False),
        (2, False),  # ints other than 0/1 fall back to default — no silent enable
        (-1, False),
        (42, False),
        ("garbage", False),  # falls back to default
        (None, False),
        ([], False),
    ],
)
def test_coerce_bool(value, expected):
    assert coerce_bool(value) is expected


def test_coerce_bool_default():
    assert coerce_bool("garbage", default=True) is True
    assert coerce_bool(None, default=True) is True
    # Out-of-range ints honour the caller-provided default
    assert coerce_bool(2, default=True) is True
    assert coerce_bool(-1, default=True) is True


def test_err_helper_formats_field_and_section_hint():
    """_err must point operators at the TOML section that owns the field."""
    assert (
        _err("pacing.ad_spots_per_break", "must be <= 5")
        == "pacing.ad_spots_per_break must be <= 5 (set in radio.toml [pacing])"
    )
    assert (
        _err("homeassistant.timer_interrupt[0].cooldown", "must be >= 1")
        == "homeassistant.timer_interrupt[0].cooldown must be >= 1 (set in radio.toml [homeassistant])"
    )
    assert _err("persona.anthem_threshold", "must be >= 1").endswith("(set in radio.toml [persona])")


def test_validate_includes_section_hint_for_invalid_pacing():
    """A bad pacing value must surface in the raised error with the [pacing] hint."""
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    config = load_config(str(toml_path))
    config.pacing.ad_spots_per_break = 99

    with pytest.raises(ValueError) as exc_info:
        _validate(config)

    msg = str(exc_info.value)
    assert "pacing.ad_spots_per_break" in msg
    assert "must be <= 5" in msg
    assert "(set in radio.toml [pacing])" in msg


def test_validate_aggregates_multiple_errors_each_with_hint():
    """Multiple invalid fields produce one line per error, each tagged with its section."""
    toml_path = Path(__file__).resolve().parents[2] / "radio.toml"
    config = load_config(str(toml_path))
    config.pacing.songs_between_banter = 1
    config.persona.anthem_threshold = 0
    config.playlist.jamendo_order = "definitely-not-valid"

    with pytest.raises(ValueError) as exc_info:
        _validate(config)

    msg = str(exc_info.value)
    assert "(set in radio.toml [pacing])" in msg
    assert "(set in radio.toml [persona])" in msg
    assert "(set in radio.toml [playlist])" in msg
