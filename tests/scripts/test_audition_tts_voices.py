from __future__ import annotations

import json

import pytest

from mammamiradio.core.config import (
    AdsSection,
    PacingSection,
    PlaylistSection,
    SonicBrandSection,
    StationConfig,
    StationSection,
)
from mammamiradio.core.models import HostPersonality, PersonalityAxes
from mammamiradio.hosts.ad_creative import AdVoice
from scripts import audition_tts_voices as audition


def _station_config() -> StationConfig:
    return StationConfig(
        station=StationSection(),
        playlist=PlaylistSection(),
        pacing=PacingSection(),
        hosts=[
            HostPersonality(
                name="Marco",
                voice="cedar",
                style="big host",
                personality=PersonalityAxes(energy=75, warmth=45, chaos=40),
                engine="openai",
                edge_fallback_voice="it-IT-GiuseppeMultilingualNeural",
            ),
            HostPersonality(
                name="Giulia",
                voice="it-IT-IsabellaNeural",
                style="dry cohost",
                personality=PersonalityAxes(energy=35, warmth=35, chaos=50),
                engine="edge",
            ),
        ],
        ads=AdsSection(
            voices=[
                AdVoice(
                    name="Roberto",
                    voice="it-IT-Alessio:DragonHDLatestNeural",
                    style="warm salesman",
                    engine="azure",
                    edge_fallback_voice="it-IT-DiegoNeural",
                ),
                AdVoice(
                    name="Rinaldo",
                    voice="it-IT-Alessio:DragonHDLatestNeural",
                    style="same timbre on purpose",
                    engine="azure",
                    edge_fallback_voice="it-IT-DiegoNeural",
                ),
            ]
        ),
        sonic_brand=SonicBrandSection(
            sweeper_voice="it-IT-IsabellaNeural",
            sweeper_engine="edge",
            sweeper_edge_fallback_voice="it-IT-GiuseppeMultilingualNeural",
        ),
    )


def _by_voice(targets: list[audition.VoiceAuditionTarget]) -> dict[tuple[str, str], audition.VoiceAuditionTarget]:
    return {(target.provider, target.voice): target for target in targets}


def test_build_audition_targets_dedupes_roles_and_catalog_voices() -> None:
    targets = audition.build_audition_targets(
        _station_config(),
        providers=["edge", "openai", "azure"],
        include_catalog=True,
    )
    by_voice = _by_voice(targets)

    cedar = by_voice[("openai", "cedar")]
    assert cedar.source == "configured+catalog"
    assert "host:Marco" in cedar.used_by
    assert "catalog:openai" in cedar.used_by
    assert cedar.edge_fallback_voice == "it-IT-GiuseppeMultilingualNeural"
    assert cedar.openai_instructions

    isabella = by_voice[("edge", "it-IT-IsabellaNeural")]
    assert "host:Giulia" in isabella.used_by
    assert "sonic_brand:sweeper" in isabella.used_by
    assert "catalog:edge" in isabella.used_by
    assert isabella.rate == "-10%"
    assert isabella.pitch == "+5Hz"

    alessio = by_voice[("azure", "it-IT-Alessio:DragonHDLatestNeural")]
    assert "ad:Roberto" in alessio.used_by
    assert "ad:Rinaldo" in alessio.used_by
    assert "catalog:azure" in alessio.used_by


def test_manual_voice_specs_support_provider_prefixed_ids_with_colons() -> None:
    specs = audition.parse_manual_voice_specs(
        [
            "openai:marin",
            "azure:it-IT-Isabella:DragonHDLatestNeural",
            "elevenlabs:voice-id-123",
        ]
    )

    assert specs == [
        ("openai", "marin"),
        ("azure", "it-IT-Isabella:DragonHDLatestNeural"),
        ("elevenlabs", "voice-id-123"),
    ]


def test_missing_env_for_provider_is_secret_safe() -> None:
    env = {"OPENAI_API_KEY": "sk-test", "AZURE_SPEECH_KEY": "azure-key"}

    assert audition.missing_env_for_provider("edge", env) == ()
    assert audition.missing_env_for_provider("openai", env) == ()
    assert audition.missing_env_for_provider("azure", env) == ("AZURE_SPEECH_REGION",)
    assert audition.missing_env_for_provider("elevenlabs", env) == ("ELEVENLABS_API_KEY",)


def test_missing_env_for_provider_honors_explicit_empty_env(monkeypatch) -> None:
    """An explicitly-empty env means 'no credentials', even when the process env
    has them — `env or os.environ` used to leak os.environ for a falsy `{}`."""
    monkeypatch.setenv("AZURE_SPEECH_KEY", "leaked")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "leaked")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "leaked")

    assert audition.missing_env_for_provider("azure", {}) == ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION")
    assert audition.missing_env_for_provider("elevenlabs", {}) == ("ELEVENLABS_API_KEY",)
    # None (the default) still falls back to the process environment.
    assert audition.missing_env_for_provider("azure", None) == ()


@pytest.mark.asyncio
async def test_run_auditions_skips_missing_cloud_credentials_without_synthesizing(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_synthesize(target: audition.VoiceAuditionTarget, output_path):
        calls.append((target.provider, target.voice))
        output_path.write_bytes(b"fake mp3")
        return output_path

    monkeypatch.setattr(audition, "_synthesize_target", fake_synthesize)
    targets = [
        audition.VoiceAuditionTarget(provider="openai", voice="cedar", label="openai", source="test"),
        audition.VoiceAuditionTarget(
            provider="edge",
            voice="it-IT-IsabellaNeural",
            label="edge",
            source="test",
        ),
    ]

    results = await audition.run_auditions(targets, tmp_path, env={}, dry_run=False)

    assert [result.status for result in results] == [audition.STATUS_SKIPPED, audition.STATUS_GENERATED]
    assert results[0].missing_env == ("OPENAI_API_KEY",)
    assert calls == [("edge", "it-IT-IsabellaNeural")]
    assert (tmp_path / "02-edge-it-IT-IsabellaNeural.mp3").read_bytes() == b"fake mp3"


@pytest.mark.asyncio
async def test_run_auditions_strict_missing_credentials_are_failures(tmp_path, monkeypatch) -> None:
    async def fail_if_called(_target: audition.VoiceAuditionTarget, _output_path):
        raise AssertionError("missing-key audition should not synthesize")

    monkeypatch.setattr(audition, "_synthesize_target", fail_if_called)

    results = await audition.run_auditions(
        [audition.VoiceAuditionTarget(provider="azure", voice="it-IT-DiegoNeural", label="azure", source="test")],
        tmp_path,
        env={},
        strict=True,
    )

    assert results[0].status == audition.STATUS_FAILED
    assert results[0].missing_env == ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION")


def test_write_manifest_records_results_without_secret_values(tmp_path) -> None:
    result = audition.VoiceAuditionResult(
        provider="openai",
        voice="cedar",
        label="openai",
        source="test",
        used_by=("catalog:openai",),
        status=audition.STATUS_SKIPPED,
        missing_env=("OPENAI_API_KEY",),
    )

    manifest = audition.write_manifest(
        [result],
        tmp_path,
        config_path=tmp_path / "radio.toml",
        timestamp="20260603T120000Z",
    )

    payload = json.loads(manifest.read_text())
    assert payload["counts"] == {audition.STATUS_SKIPPED: 1}
    assert payload["results"][0]["missing_env"] == ["OPENAI_API_KEY"]
    assert "sk-test" not in manifest.read_text()
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        audition.write_manifest([result], tmp_path, config_path=tmp_path / "radio.toml", timestamp="20260603T120000Z")


def test_cli_dry_run_lists_all_openai_catalog_without_writing_files(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(audition, "load_config", lambda _path: _station_config())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    rc = audition.main(
        [
            "--providers",
            "openai",
            "--include-catalog",
            "--dry-run",
            "--output-dir",
            str(tmp_path),
            "--timestamp",
            "20260603T120000Z",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "Dry-run targets:" in captured.out
    assert "planned\topenai\tcedar" in captured.out
    assert "planned\topenai\tmarin" in captured.out
    assert list(tmp_path.iterdir()) == []


def test_cli_rejects_bad_manual_voice_spec(tmp_path, capsys) -> None:
    rc = audition.main(["--voice", "cedar", "--output-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert rc == 2
    assert "provider:voice_id" in captured.err
